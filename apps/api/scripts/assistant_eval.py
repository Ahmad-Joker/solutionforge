"""Run the assistant agent-behaviour evals and write per-case results.

    python scripts/assistant_eval.py --dataset dev --models fixture:policy-v1
    python scripts/assistant_eval.py --dataset dev --models anthropic:claude-haiku-5-5 \\
        --limit 3 --max-requests 15 --max-spend-usd 0.05

- ``fixture:*`` runs are labelled FIXTURE: they check the harness, not a model.
- Live models need ``SF_ANTHROPIC_API_KEY`` (or ``ANTHROPIC_API_KEY``). Without one, live
  cases are recorded as NOT RUN with reason BLOCKED; nothing is substituted.
- ``--max-requests`` / ``--max-spend-usd`` are hard caps across the whole invocation. A
  case only starts if it cannot exceed them (worst case: max_turns x (1 + max_retries)
  requests; spend is capped per run by the remaining budget). Skipped cases are NOT RUN.
- The held-out set needs ``--confirm-holdout`` and every run is appended to
  ``evals/assistant/HOLDOUT_LOG.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import platform
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from solutionforge.assistant.evals import (
    DIMENSIONS,
    EvalCase,
    Observation,
    load_cases,
    score,
    summarize,
)
from solutionforge.assistant.harness import API_ROOT, Env, KBSpec, build_env
from solutionforge.assistant.service import PROMPT_VERSION, AssistantConfig
from solutionforge.domain import AssistantEvent, Chunk, Document, SimTicket, TicketDraft
from solutionforge.llm.pricing import PRICES_ACCESSED, PRICES_SOURCE_URL, usd_to_micro


def _repo_root() -> Path:
    """The checkout containing evals/assistant, found from the working directory (the
    package path may be a redirected or installed copy)."""
    for p in (Path.cwd(), *Path.cwd().parents, API_ROOT.parents[1]):
        if (p / "evals" / "assistant").is_dir():
            return p
    raise SystemExit("run from inside the repository (evals/assistant not found)")


REPO = _repo_root()
EVALS = REPO / "evals" / "assistant"


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--dataset", default="dev", help="dev | holdout | path to a .jsonl file")
    p.add_argument("--models", default="fixture:policy-v1", help="comma-separated provider:model")
    p.add_argument("--tool-mode", choices=["model", "fixed"], default="model")
    p.add_argument("--embedder", default="hashing-v1")
    p.add_argument("--chunk-size", type=int, default=800)
    p.add_argument("--chunk-overlap", type=int, default=120)
    p.add_argument("--top-k", type=int, default=4)
    p.add_argument("--strategy", choices=["hybrid", "dense", "keyword"], default="hybrid")
    p.add_argument("--limit", type=int, default=None, help="first N cases only (smoke test)")
    p.add_argument("--cases", default=None, help="comma-separated case IDs")
    p.add_argument("--max-requests", type=int, default=None, help="cap on provider requests")
    p.add_argument("--max-spend-usd", type=float, default=None, help="cap on estimated spend")
    p.add_argument("--database-url", default=None, help="default: a throwaway SQLite file")
    p.add_argument(
        "--out", default=None, help="results folder (default: evals/assistant/results/...)"
    )
    p.add_argument("--label", default="", help="short note stored with the results")
    p.add_argument("--confirm-holdout", action="store_true")
    return p.parse_args(argv)


class Caps:
    def __init__(self, max_requests: int | None, max_spend_usd: float | None) -> None:
        self.max_requests = max_requests
        self.max_spend = usd_to_micro(Decimal(str(max_spend_usd))) if max_spend_usd else None
        self.requests = 0
        self.spend = 0

    def blocked_reason(self, worst_requests: int) -> str | None:
        if self.max_requests is not None and self.requests + worst_requests > self.max_requests:
            return f"request cap ({self.max_requests}) would be exceeded"
        if self.max_spend is not None and self.spend >= self.max_spend:
            return "spend cap reached"
        return None

    def remaining_spend(self) -> int | None:
        return None if self.max_spend is None else self.max_spend - self.spend


async def run_case(
    env: Env, case: EvalCase, model: str, args: argparse.Namespace, caps: Caps
) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "model": model,
        "tool_mode": args.tool_mode,
        "top_k": args.top_k,
        "retrieval_strategy": args.strategy,
    }
    cfg = AssistantConfig.from_settings(env.settings, **overrides)
    remaining = caps.remaining_spend()
    if remaining is not None:  # never let one run spend past the invocation's cap
        overrides["run_budget_micro_usd"] = max(1, min(cfg.run_budget_micro_usd, remaining))
        cfg = AssistantConfig.from_settings(env.settings, **overrides)
    sm = env.app.state.sessionmaker
    async with sm() as s:
        tickets_before = await s.scalar(sa.select(sa.func.count()).select_from(SimTicket))
    run = await env.assistant.run(
        env.ctx, question=case.input, knowledge_base_id=env.kbs[case.knowledge_base], config=cfg
    )
    async with sm() as s:
        tickets_after = await s.scalar(sa.select(sa.func.count()).select_from(SimTicket))
        events = (
            await s.scalars(
                sa.select(AssistantEvent)
                .where(AssistantEvent.run_id == run.id)
                .order_by(AssistantEvent.seq)
            )
        ).all()
        drafts = (await s.scalars(sa.select(TicketDraft).where(TicketDraft.run_id == run.id))).all()
        passages = []
        for c in run.citations:
            text = await s.scalar(
                sa.select(Chunk.text)
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    Document.knowledge_base_id == env.kbs[case.knowledge_base],
                    Document.doc_metadata["doc_id"].as_string() == c["doc_id"],
                    Chunk.ordinal == int(c["source_id"].split("#")[1]),
                )
            )
            passages.append(f"[{c['source_id']}] {text or ''}")
    tool_events = [
        {"name": e.name, "outcome": e.outcome, **e.detail} for e in events if e.kind == "tool"
    ]
    for e in tool_events:  # flatten args for scoring
        e["args"] = e.get("args", {})
    invalid = [i for e in events if e.kind == "check" for i in e.detail.get("invalid", [])]
    obs = Observation(
        run_status=run.status,
        answer=run.answer or "",
        citations=[c["source_id"] for c in run.citations],
        invalid_citations=invalid,
        tool_events=tool_events,
        draft_statuses=[d.status for d in drafts],
        tickets_created=(tickets_after or 0) - (tickets_before or 0),
        error=run.error,
    )
    sc = score(case, obs)
    provider_requests = sum(1 for e in events if e.kind == "provider")
    caps.requests += provider_requests
    caps.spend += run.cost_micro_usd or 0
    return {
        "case_id": case.id,
        "category": case.category,
        "model": model,
        "verdict": sc.verdict,
        "dimensions": sc.dimensions,
        "reasons": sc.reasons,
        "input": case.input,
        "answer": run.answer,
        "citations": obs.citations,
        "invalid_citations": invalid,
        "tool_trace": [
            {k: v for k, v in e.items() if k in ("name", "outcome", "args", "results", "status")}
            for e in tool_events
        ],
        "run_id": str(run.id),
        "run_status": run.status,
        "run_error": run.error,
        "turns": run.turns,
        "tool_calls": run.tool_calls,
        "retries": run.retries,
        "provider_requests": provider_requests,
        "latency_ms": run.latency_ms,
        "retrieval_ms": run.retrieval_ms,
        "tool_ms": run.tool_ms,
        "usage": {
            "input": run.input_tokens,
            "output": run.output_tokens,
            "cache_read": run.cache_read_tokens,
            "cache_write_5m": run.cache_write_tokens,
            "cache_write_1h": run.cache_write_1h_tokens,
        },
        "cost_micro_usd": run.cost_micro_usd,
        "tickets_created": obs.tickets_created,
        "draft_statuses": obs.draft_statuses,
        "corpus_version": run.corpus_version,
        "_passages": passages,
    }


def not_run(case: EvalCase, model: str, reason: str) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "category": case.category,
        "model": model,
        "verdict": "NOT RUN",
        "dimensions": dict.fromkeys(DIMENSIONS),
        "reasons": [reason],
        "input": case.input,
        "run_status": None,
        "latency_ms": None,
        "cost_micro_usd": None,
    }


def md_row(*cells: object) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def dash(x: object) -> str:
    return "—" if x is None else str(x)


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def fmt_usd(micro: int | None) -> str:
    return "unknown" if micro is None else f"${micro / 1_000_000:.4f}"


def report(
    meta: dict[str, Any], summaries: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
) -> str:
    lines = [f"# Assistant eval: {meta['dataset']} ({meta['mode']})", ""]
    if meta["mode"] == "FIXTURE":
        lines += [
            "> **FIXTURE run.** `fixture:policy-v1` is a deterministic rule-based policy, not a",
            "> language model. These results check the harness and scoring; they are not model",
            "> results and must not be quoted as such.",
            "",
        ]
    lines += ["| Setting | Value |", "|---|---|"]
    for k in (
        "started",
        "models",
        "tool_mode",
        "embedder",
        "chunking",
        "top_k",
        "retrieval_strategy",
        "prompt_version",
        "corpus_version",
        "caps",
        "database",
        "label",
    ):
        lines.append(f"| {k} | {meta.get(k, '')} |")
    lines += [
        "",
        "## Summary",
        "",
        md_row(
            "Model",
            "Pass",
            "Fail",
            "Not run",
            "Pass rate",
            "Median latency (ms)",
            "p95 latency (ms)",
            "n",
            "Total cost",
            "Avg cost / attempt",
            "Cost / success",
            "Failed runs",
            "Limit reached",
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for model, s in summaries.items():
        lat = s["latency_ms"]
        lines.append(
            md_row(
                model,
                s["passed"],
                s["failed"],
                s["not_run"],
                fmt_pct(s["pass_rate"]),
                dash(lat["median"]),
                dash(lat["p95"]),
                lat["n"],
                fmt_usd(s["cost"]["total_micro_usd"]),
                fmt_usd(s["cost"]["avg_per_attempt_micro_usd"]),
                fmt_usd(s["cost"]["per_success_micro_usd"]),
                s["run_errors"],
                s["limit_reached"],
            )
        )
    lines += [
        "",
        "Latency percentiles use nearest rank. With n ≤ 30 the p95 is close to the maximum",
        "and is not a stable estimate.",
        "",
        "Costs are **estimates** from list prices "
        f"([source]({PRICES_SOURCE_URL}), accessed {PRICES_ACCESSED}), not an invoice.",
        "",
    ]
    for model, s in summaries.items():
        lines += [
            f"### {model}: dimensions",
            "",
            "| Dimension | Passed / applicable |",
            "|---|---|",
        ]
        for d, v in s["dimensions"].items():
            lines.append(f"| {d} | {v['passed']} / {v['applicable']} |")
        lines.append("| semantic grounding (human review) | NOT RUN |")
        lines += ["", "| Category | Passed / ran | Not run |", "|---|---|---|"]
        for c, v in s["categories"].items():
            lines.append(f"| {c} | {v['passed']} / {v['ran']} | {v['not_run']} |")
        lines.append("")
    lines += [
        "## Cases",
        "",
        "| Model | Case | Verdict | Run status | Reasons |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        reasons = "; ".join(r["reasons"])[:300].replace("|", "\\|")
        lines.append(
            md_row(r["model"], r["case_id"], r["verdict"], r.get("run_status") or "—", reasons)
        )
    return "\n".join(lines) + "\n"


async def main(argv: list[str]) -> int:
    args = parse(argv)
    path = (
        EVALS / f"{args.dataset}.jsonl"
        if args.dataset in ("dev", "holdout")
        else Path(args.dataset)
    )
    holdout = path.name.startswith("holdout")
    if holdout and not args.confirm_holdout:
        print("refusing to run the held-out set without --confirm-holdout", file=sys.stderr)
        return 2
    cases = load_cases(path)
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c.id in wanted]
    if args.limit:
        cases = cases[: args.limit]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    live = [m for m in models if not m.startswith("fixture:")]
    key = os.environ.get("SF_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    mode = "FIXTURE" if not live else ("LIVE" if key else "BLOCKED")
    if live and (args.max_requests is None or args.max_spend_usd is None):
        print("live runs need --max-requests and --max-spend-usd", file=sys.stderr)
        return 2

    env = await build_env(
        [
            KBSpec("core", args.chunk_size, args.chunk_overlap, args.embedder),
            KBSpec("injection", args.chunk_size, args.chunk_overlap, args.embedder, True),
        ],
        database_url=args.database_url,
        anthropic_api_key=key if live else None,
    )
    caps = Caps(args.max_requests, args.max_spend_usd)
    started = datetime.now(UTC)
    # Short folder names: deep checkouts on Windows hit the 260-character path limit. Mode,
    # models and settings are recorded inside report.md and summary.json.
    short = f"{started:%m%d-%H%M%S}-{'hold' if holdout else path.stem[:8]}"
    short += "-fw" if args.tool_mode == "fixed" else ""
    out = Path(args.out) if args.out else EVALS / "results" / short
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    try:
        for model in models:
            is_live = not model.startswith("fixture:")
            for case in cases:
                if is_live and not key:
                    rows.append(not_run(case, model, "BLOCKED: no Anthropic API key configured"))
                    continue
                worst = env.settings.assistant_max_turns * (1 + env.settings.assistant_max_retries)
                reason = caps.blocked_reason(worst) if is_live else None
                if reason:
                    rows.append(not_run(case, model, reason))
                    continue
                try:
                    rows.append(await run_case(env, case, model, args, caps))
                except Exception as exc:
                    r = not_run(case, model, f"harness error: {type(exc).__name__}: {exc}"[:300])
                    r["verdict"] = "FAIL"
                    r["run_status"] = "harness_error"
                    r["latency_ms"] = 0
                    rows.append(r)
                print(f"{model} {case.id}: {rows[-1]['verdict']}", flush=True)
    finally:
        await env.close()

    summaries = {m: summarize([r for r in rows if r["model"] == m]) for m in models}
    corpus_versions = sorted({r.get("corpus_version") for r in rows if r.get("corpus_version")})
    meta = {
        "dataset": path.name,
        "mode": mode,
        "started": started.isoformat(timespec="seconds"),
        "models": ", ".join(models),
        "tool_mode": args.tool_mode,
        "embedder": args.embedder,
        "chunking": f"{args.chunk_size}/{args.chunk_overlap}",
        "top_k": args.top_k,
        "retrieval_strategy": args.strategy,
        "prompt_version": PROMPT_VERSION,
        "corpus_version": ", ".join(corpus_versions),
        "caps": (
            f"requests={args.max_requests} spend_usd={args.max_spend_usd} "
            f"(used: {caps.requests} requests, {fmt_usd(caps.spend)})"
        ),
        "database": "SQLite (throwaway)"
        if args.database_url is None
        else "PostgreSQL"
        if "postgresql" in args.database_url
        else "custom",
        "label": args.label,
        "python": platform.python_version(),
        "pricing_source": PRICES_SOURCE_URL,
        "pricing_accessed": PRICES_ACCESSED,
    }
    with (out / "cases.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(
                json.dumps({k: v for k, v in r.items() if k != "_passages"}, default=str) + "\n"
            )
    (out / "summary.json").write_text(
        json.dumps({"meta": meta, "summaries": summaries}, indent=2, default=str) + "\n", "utf-8"
    )
    (out / "report.md").write_text(report(meta, summaries, rows), "utf-8")
    with (out / "review.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "model",
                "case_id",
                "question",
                "answer",
                "cited_passages",
                "supported",
                "reviewer",
                "notes",
            ]
        )
        for r in rows:
            if r["verdict"] != "NOT RUN" and r.get("_passages"):
                w.writerow(
                    [
                        r["model"],
                        r["case_id"],
                        r["input"],
                        r.get("answer") or "",
                        "\n".join(r["_passages"]),
                        "",
                        "",
                        "",
                    ]
                )
    if holdout:
        rel = out.relative_to(REPO) if out.is_relative_to(REPO) else out
        with (EVALS / "HOLDOUT_LOG.md").open("a", encoding="utf-8", newline="\n") as f:
            when = f"{started:%Y-%m-%d %H:%M}"
            entry = md_row(when, ", ".join(models), mode, f"`{rel.as_posix()}`", args.label)
            f.write(entry + "\n")
    for m, s in summaries.items():
        print(f"{m}: {s['passed']} pass / {s['failed']} fail / {s['not_run']} not run")
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
