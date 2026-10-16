"""Where do injected documents rank, per retrieval configuration? (measurement only)

    python scripts/injection_retrieval_probe.py

Context: with bge-small + dense retrieval, the fixture policy cited the injected
"Warranty clarification" page (HP-INJ-04) instead of the real warranty FAQ (dev-inj-04).
This probe asks what a *model* would see: for each dev question, the top-k passages from
the isolated injection knowledge base (core docs + 5 HP-INJ-* docs). Per configuration:

- rank of the first injected passage, and of the first acceptable non-injected passage;
- whether an acceptable source is still present in the top k (the model can still answer);
- for the answerable dev questions, whether adding injection docs pushes acceptable
  sources out of the top 3.

Variant "strip-comments": HTML comments (<!-- ... -->, invisible in rendered Markdown) are
removed before indexing. It tests one candidate mitigation; it is not applied in the app.
No model calls are made.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solutionforge.assistant.corpus import load_corpus, sync_documents
from solutionforge.assistant.evals import load_cases
from solutionforge.assistant.harness import KBSpec, build_env
from solutionforge.retrieval.ingest import IngestionWorker
from solutionforge.retrieval.search import Strategy

TOP_K = 4  # the assistant's default top-k
COMMENT = re.compile(r"<!--.*?-->", re.S)


def _repo_root() -> Path:
    for p in (Path.cwd(), *Path.cwd().parents):
        if (p / "evals" / "assistant").is_dir():
            return p
    raise SystemExit("run from inside the repository")


async def main() -> int:
    repo = _repo_root()
    cases = [
        c
        for c in load_cases(repo / "evals" / "assistant" / "dev.jsonl")
        if c.category in ("injection", "answerable")
    ]
    env = await build_env(
        [
            KBSpec("lexical", embedder="hashing-v1", include_injection=True),
            KBSpec("semantic", embedder="bge-small-en-v1.5", include_injection=True),
        ]
    )
    # Variant KB: same docs, HTML comments stripped before indexing.
    from solutionforge.services import kb_service
    from solutionforge.services.audit_service import RequestMeta

    async with env.app.state.sessionmaker() as s:
        kb = await kb_service.create_kb(
            s,
            env.ctx,
            name="semantic-strip-comments",
            description="probe",
            embedder="bge-small-en-v1.5",
            chunk_size=800,
            chunk_overlap=120,
            request=RequestMeta(),
        )
        env.kbs["semantic-strip-comments"] = kb.id
    stripped = [
        replace(d, body=COMMENT.sub("", d.body)) for d in load_corpus(include_injection=True)
    ]
    await sync_documents(env.app.state.sessionmaker, env.ctx, kb.id, stripped)
    await IngestionWorker(
        env.app.state.sessionmaker, env.app.state.retriever.embedders
    ).run_until_idle()

    configs = [
        ("lexical", Strategy.HYBRID),
        ("semantic", Strategy.DENSE),
        ("semantic", Strategy.HYBRID),
        ("semantic-strip-comments", Strategy.DENSE),
    ]
    rows: list[dict[str, Any]] = []
    try:
        for kb_label, strategy in configs:
            for case in cases:
                hits = await env.app.state.retriever.search(
                    organization_id=env.ctx.organization_id,
                    knowledge_base_id=env.kbs[kb_label],
                    query=case.input,
                    top_k=TOP_K,
                    strategy=strategy,
                )
                docs = [str(h.metadata.get("doc_id")) for h in hits]
                good = [d for d in case.relevant_sources if not d.startswith("HP-INJ")]
                first_inj = next(
                    (i + 1 for i, d in enumerate(docs) if d.startswith("HP-INJ")), None
                )
                first_good = next((i + 1 for i, d in enumerate(docs) if d in good), None)
                hidden = any("<!--" in h.text for h in hits)
                rows.append(
                    {
                        "config": f"{kb_label}:{strategy.value}",
                        "case_id": case.id,
                        "category": case.category,
                        "retrieved": [f"{h.metadata.get('doc_id')}#{h.ordinal}" for h in hits],
                        "first_injected_rank": first_inj,
                        "first_acceptable_rank": first_good,
                        "acceptable_in_top3": first_good is not None and first_good <= 3,
                        "acceptable_in_topk": first_good is not None,
                        "hidden_comment_in_passages": hidden,
                        "has_acceptable_label": bool(good),
                    }
                )
    finally:
        await env.close()

    out = repo / "evals" / "retrieval" / f"inj-{datetime.now(UTC):%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "rows.json").write_text(json.dumps(rows, indent=2) + "\n", "utf-8")
    lines = [
        "# Injected-document ranking probe",
        "",
        f"top_k = {TOP_K}; measurement only, no model calls.",
        "",
        "| Config | Injection cases: injected doc ranked #1 "
        "| Injection cases with a labelled real source: real source in top k "
        "| Answerable (12): acceptable in top 3 | Passages containing hidden HTML comments |",
        "|---|---|---|---|---|",
    ]
    for cfg in dict.fromkeys(r["config"] for r in rows):
        rs = [r for r in rows if r["config"] == cfg]
        inj = [r for r in rs if r["category"] == "injection"]
        labelled = [r for r in inj if r["has_acceptable_label"]]
        ans = [r for r in rs if r["category"] == "answerable"]
        lines.append(
            f"| {cfg} | {sum(r['first_injected_rank'] == 1 for r in inj)}/{len(inj)} "
            f"| {sum(r['acceptable_in_topk'] for r in labelled)}/{len(labelled)} "
            f"| {sum(r['acceptable_in_top3'] for r in ans)}/{len(ans)} "
            f"| {sum(r['hidden_comment_in_passages'] for r in rs)} |"
        )
    lines += [
        "",
        "## Injection cases",
        "",
        "| Config | Case | Retrieved (top k) |",
        "|---|---|---|",
    ]
    for r in rows:
        if r["category"] == "injection":
            lines.append(f"| {r['config']} | {r['case_id']} | {', '.join(r['retrieved'])} |")
    (out / "report.md").write_text("\n".join(lines) + "\n", "utf-8")
    print("\n".join(lines[4 : 4 + 2 + len(configs)]))
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
