"""Summarise a k6 JSON summary (from load/run.sh) as a Markdown table."""

import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
m = data["metrics"]


def row(name: str, key: str) -> str | None:
    if key not in m:
        return None
    v = m[key]["values"]
    return (f"| {name} | {v.get('med', 0):.0f} | {v.get('p(95)', 0):.0f} | {v.get('p(99)', 0):.0f} "
            f"| {v.get('max', 0):.0f} |")


print("| endpoint | p50 ms | p95 ms | p99 ms | max ms |")
print("|---|---|---|---|---|")
for tag in ("list_workflows", "list_executions", "get_execution", "start_execution", "search_hybrid"):
    r = row(tag, f"http_req_duration{{name:{tag}}}")
    if r:
        print(r)
reqs = m["http_reqs"]["values"]
failed = m["http_req_failed"]["values"]
print(f"\nrequests: {reqs['count']:.0f} ({reqs['rate']:.1f}/s), failed: {failed['rate'] * 100:.2f}%")
if "executions_started" in m:
    print(f"executions started: {m['executions_started']['values']['count']:.0f}")
if "dropped_iterations" in m:
    print(f"dropped iterations (generator could not keep the arrival rate): "
          f"{m['dropped_iterations']['values']['count']:.0f}")
