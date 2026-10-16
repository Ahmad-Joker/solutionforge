"""Compare retrieval configurations on the assistant's dev questions.

    python scripts/retrieval_eval.py                       # 2 chunkings x 2 embedders
    python scripts/retrieval_eval.py --configs hashing-v1:800:120,hashing-v1:400:60

Questions: the dev cases that have acceptable source documents labelled in advance
(``relevant_sources`` in evals/assistant/dev.jsonl, answerable category only). The held-out
set is never used here.

Metric: **hit rate @k** (default k=3): the share of questions where at least one of the
top-k retrieved chunks belongs to an acceptable document. This is not recall: questions
may have several acceptable documents and only one needs to appear. Retrieval latency is
the time of the search call alone (embedding the query + database search + fusion), reported
separately from any end-to-end latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solutionforge.assistant.evals import load_cases, percentile
from solutionforge.assistant.harness import KBSpec, build_env
from solutionforge.retrieval.search import Strategy


def _repo_root() -> Path:
    for p in (Path.cwd(), *Path.cwd().parents):
        if (p / "evals" / "assistant").is_dir():
            return p
    raise SystemExit("run from inside the repository")


REPO = _repo_root()


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument(
        "--configs",
        default="hashing-v1:800:120,hashing-v1:200:30,bge-small-en-v1.5:800:120,bge-small-en-v1.5:200:30",
        help="comma-separated embedder:chunk_size:chunk_overlap",
    )
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--strategies", default="hybrid", help="comma-separated: hybrid,dense,keyword")
    p.add_argument("--repeats", type=int, default=3, help="timed repetitions per query")
    p.add_argument("--database-url", default=None)
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


async def main(argv: list[str]) -> int:
    args = parse(argv)
    cases = [
        c
        for c in load_cases(REPO / "evals" / "assistant" / "dev.jsonl")
        if c.category == "answerable" and c.relevant_sources
    ]
    specs = []
    for raw in args.configs.split(","):
        emb, size, overlap = raw.split(":")
        specs.append(KBSpec(f"{emb}:{size}:{overlap}", int(size), int(overlap), emb))
    t0 = time.perf_counter()
    env = await build_env(
        specs,
        database_url=args.database_url,
        embedding_cache_dir=os.environ.get("SF_EMBEDDING_CACHE_DIR"),
    )
    setup_s = time.perf_counter() - t0
    retriever = env.app.state.retriever
    results: dict[str, Any] = {}
    try:
        runs = [(spec, Strategy(st)) for spec in specs for st in args.strategies.split(",")]
        for spec, strategy in runs:
            label = f"{spec.label}:{strategy.value}"
            kb = env.kbs[spec.label]
            # Warm-up (model load, caches) so timings measure steady-state search.
            await retriever.search(
                organization_id=env.ctx.organization_id, knowledge_base_id=kb, query="warm up"
            )
            per_case = []
            for case in cases:
                timings = []
                hits = []
                for _ in range(args.repeats):
                    t = time.perf_counter()
                    hits = await retriever.search(
                        organization_id=env.ctx.organization_id,
                        knowledge_base_id=kb,
                        query=case.input,
                        top_k=args.k,
                        strategy=strategy,
                    )
                    timings.append((time.perf_counter() - t) * 1000)
                docs = [str(h.metadata.get("doc_id")) for h in hits]
                per_case.append(
                    {
                        "case_id": case.id,
                        "question": case.input,
                        "acceptable": case.relevant_sources,
                        "retrieved": [f"{h.metadata.get('doc_id')}#{h.ordinal}" for h in hits],
                        "hit": any(d in case.relevant_sources for d in docs),
                        "latency_ms": round(statistics.median(timings), 2),
                    }
                )
            lat = [r["latency_ms"] for r in per_case]
            async with env.app.state.sessionmaker() as s:
                from sqlalchemy import func, select

                from solutionforge.domain import Chunk

                chunks = await s.scalar(
                    select(func.count()).select_from(Chunk).where(Chunk.knowledge_base_id == kb)
                )
            results[label] = {
                "embedder": spec.embedder,
                "strategy": strategy.value,
                "chunk_size": spec.chunk_size,
                "chunk_overlap": spec.chunk_overlap,
                "chunks": chunks,
                "hits": sum(r["hit"] for r in per_case),
                "n": len(per_case),
                f"hit_rate_at_{args.k}": round(sum(r["hit"] for r in per_case) / len(per_case), 4),
                "latency_ms_median": round(statistics.median(lat), 2),
                "latency_ms_p95": percentile(lat, 0.95),
                "cases": per_case,
            }
            print(
                f"{label}: hit@{args.k} {results[label]['hits']}/{len(per_case)} "
                f"median {results[label]['latency_ms_median']} ms",
                flush=True,
            )
    finally:
        await env.close()

    started = datetime.now(UTC)
    out = Path(args.out) if args.out else REPO / "evals" / "retrieval" / f"{started:%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "when": started.isoformat(timespec="seconds"),
        "questions": len(cases),
        "question_source": "evals/assistant/dev.jsonl (answerable, labelled sources)",
        "k": args.k,
        "strategies": args.strategies + " (hybrid = dense + keyword, reciprocal rank fusion)",
        "database": "SQLite (throwaway; dense search is brute-force in Python, not pgvector)"
        if args.database_url is None
        else "PostgreSQL + pgvector",
        "repeats": args.repeats,
        "setup_seconds_including_ingestion": round(setup_s, 1),
    }
    (out / "results.json").write_text(
        json.dumps({"meta": meta, "configs": results}, indent=2) + "\n", "utf-8"
    )
    lines = [
        "# Retrieval comparison",
        "",
        *[f"- **{k}**: {v}" for k, v in meta.items()],
        "",
        "| Config (embedder:size:overlap:strategy) | Chunks | Hits "
        f"| Hit rate @{args.k} | Median latency (ms) | p95 latency (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for label, r in results.items():
        lines.append(
            f"| {label} | {r['chunks']} | {r['hits']}/{r['n']} | {r[f'hit_rate_at_{args.k}']:.2f} "
            f"| {r['latency_ms_median']} | {r['latency_ms_p95']} |"
        )
    lines += [
        "",
        f"n = {len(cases)} questions: a small sample, so one question is "
        f"{100 / len(cases):.0f} points of hit rate.",
        "",
        "## Misses",
        "",
    ]
    for label, r in results.items():
        for c in r["cases"]:
            if not c["hit"]:
                lines.append(
                    f"- `{label}` {c['case_id']}: wanted {c['acceptable']}, got {c['retrieved']}"
                )
    (out / "report.md").write_text("\n".join(lines) + "\n", "utf-8")
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
