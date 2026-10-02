"""Median and range across several k6 runs of the same profile (load/results/*.json)."""

import json
import statistics
import sys

runs = [json.load(open(p, encoding="utf-8"))["metrics"] for p in sys.argv[1:]]
tags = ("list_workflows", "list_executions", "get_execution", "start_execution", "search_hybrid")
print(f"{len(runs)} runs. Cells: median across runs (min–max).\n")
print("| endpoint | p50 ms | p95 ms | p99 ms |\n|---|---|---|---|")
for t in tags:
    key = f"http_req_duration{{name:{t}}}"
    cells = []
    for stat in ("med", "p(95)", "p(99)"):
        vals = [r[key]["values"][stat] for r in runs]
        cells.append(f"{statistics.median(vals):.0f} ({min(vals):.0f}–{max(vals):.0f})")
    print(f"| {t} | " + " | ".join(cells) + " |")
rates = [r["http_reqs"]["values"]["rate"] for r in runs]
fails = [r["http_req_failed"]["values"]["rate"] * 100 for r in runs]
dropped = [r.get("dropped_iterations", {"values": {"count": 0}})["values"]["count"] for r in runs]
print(f"\nthroughput req/s: {statistics.median(rates):.1f} ({min(rates):.1f}–{max(rates):.1f}); "
      f"failed: {max(fails):.2f}% worst run; dropped iterations: {min(dropped):.0f}–{max(dropped):.0f}")
