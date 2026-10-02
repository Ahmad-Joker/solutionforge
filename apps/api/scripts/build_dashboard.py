"""Generate the Grafana dashboard (dashboards as code).

    python scripts/build_dashboard.py            # writes infra/obs/dashboards/solutionforge.json
    python scripts/build_dashboard.py --check    # exit 1 if the committed file is stale

Every PromQL expression here is also checked by the test suite against the metrics the
app actually registers, so a renamed metric breaks CI instead of silently breaking a panel.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[3] / "infra" / "obs" / "dashboards" / "solutionforge.json"
DS = {"type": "prometheus", "uid": "prometheus"}

# (title, unit, [(expr, legend)]) per panel; rows of up to 3 panels.
ROWS: list[tuple[str, list[tuple[str, str, list[tuple[str, str]]]]]] = [
    (
        "HTTP API",
        [
            (
                "Requests / s by route",
                "reqps",
                [
                    ("sum by (route) (rate(sf_http_requests_total[5m]))", "{{route}}"),
                ],
            ),
            (
                "5xx error ratio",
                "percentunit",
                [
                    (
                        'sum(rate(sf_http_requests_total{status=~"5.."}[5m]))'
                        " / clamp_min(sum(rate(sf_http_requests_total[5m])), 1e-9)",
                        "5xx",
                    ),
                ],
            ),
            (
                "p95 latency by route",
                "s",
                [
                    (
                        "histogram_quantile(0.95, sum by (le, route) "
                        "(rate(sf_http_request_duration_seconds_bucket[5m])))",
                        "{{route}}",
                    ),
                ],
            ),
        ],
    ),
    (
        "Workflows",
        [
            (
                "Executions by status (live)",
                "short",
                [
                    ("max by (status) (sf_workflow_executions)", "{{status}}"),
                ],
            ),
            (
                "Finished / min by status",
                "short",
                [
                    (
                        "sum by (status) (rate(sf_workflow_executions_finished_total[5m])) * 60",
                        "{{status}}",
                    ),
                ],
            ),
            (
                "Oldest runnable queued execution",
                "s",
                [
                    ("max(sf_workflow_oldest_queued_seconds)", "age"),
                ],
            ),
            (
                "p95 step duration by type",
                "s",
                [
                    (
                        "histogram_quantile(0.95, sum by (le, step_type) "
                        "(rate(sf_workflow_step_duration_seconds_bucket[5m])))",
                        "{{step_type}}",
                    ),
                ],
            ),
            (
                "Step attempts by outcome",
                "short",
                [
                    (
                        "sum by (status) (rate(sf_workflow_step_duration_seconds_count[5m])) * 60",
                        "{{status}}",
                    ),
                ],
            ),
        ],
    ),
    (
        "LLM",
        [
            (
                "Calls / min by outcome",
                "short",
                [
                    ("sum by (outcome) (rate(sf_llm_calls_total[5m])) * 60", "{{outcome}}"),
                ],
            ),
            (
                "Tokens / min by model",
                "short",
                [
                    (
                        "sum by (model, direction) (rate(sf_llm_tokens_total[5m])) * 60",
                        "{{model}} {{direction}}",
                    ),
                ],
            ),
            (
                "Spend (USD / hour)",
                "currencyUSD",
                [
                    ("sum by (model) (rate(sf_llm_cost_usd_total[1h])) * 3600", "{{model}}"),
                ],
            ),
            (
                "p95 call latency by model",
                "s",
                [
                    (
                        "histogram_quantile(0.95, sum by (le, model) "
                        "(rate(sf_llm_call_duration_seconds_bucket[5m])))",
                        "{{model}}",
                    ),
                ],
            ),
        ],
    ),
    (
        "Tools, approvals, knowledge, evaluation",
        [
            (
                "Tool calls / min by outcome",
                "short",
                [
                    ("sum by (outcome) (rate(sf_tool_calls_total[5m])) * 60", "{{outcome}}"),
                ],
            ),
            (
                "Policy-gated tool calls / min",
                "short",
                [
                    (
                        'sum by (tool) (rate(sf_tool_calls_total{outcome=~"tool_denied|'
                        'tool_approval_required"}[5m])) * 60',
                        "{{tool}}",
                    ),
                ],
            ),
            (
                "p95 tool latency",
                "s",
                [
                    (
                        "histogram_quantile(0.95, sum by (le, tool) "
                        "(rate(sf_tool_call_duration_seconds_bucket[5m])))",
                        "{{tool}}",
                    ),
                ],
            ),
            ("Pending approvals", "short", [("max(sf_approvals_pending)", "pending")]),
            (
                "Ingestion backlog",
                "short",
                [
                    ("max by (status) (sf_documents)", "{{status}}"),
                ],
            ),
            (
                "Evaluation runs & gate decisions",
                "short",
                [
                    ("max(sf_evaluation_runs_running)", "runs in progress"),
                    (
                        "sum by (result) (increase(sf_deployment_gate_decisions_total[1h]))",
                        "gate {{result}} / h",
                    ),
                ],
            ),
        ],
    ),
]


def build() -> dict[str, Any]:
    panels: list[dict[str, Any]] = []
    y, pid = 0, 1
    for row_title, row_panels in ROWS:
        panels.append(
            {
                "type": "row",
                "title": row_title,
                "id": pid,
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            }
        )
        pid += 1
        y += 1
        for i, (title, unit, targets) in enumerate(row_panels):
            panels.append(
                {
                    "type": "timeseries",
                    "id": pid,
                    "title": title,
                    "datasource": DS,
                    "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
                    "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
                    "gridPos": {"h": 8, "w": 8, "x": (i % 3) * 8, "y": y + (i // 3) * 8},
                    "targets": [
                        {"refId": chr(65 + j), "datasource": DS, "expr": e, "legendFormat": leg}
                        for j, (e, leg) in enumerate(targets)
                    ],
                }
            )
            pid += 1
        y += 8 * ((len(row_panels) + 2) // 3)
    return {
        "uid": "solutionforge-overview",
        "title": "SolutionForge — overview",
        "tags": ["solutionforge"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels,
    }


def expressions() -> list[str]:
    return [e for _, ps in ROWS for _, _, ts in ps for e, _ in ts]


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


if __name__ == "__main__":
    if "--check" in sys.argv:
        stale = not OUT.exists() or OUT.read_text(encoding="utf-8") != render()
        print("stale" if stale else "up to date")
        sys.exit(1 if stale else 0)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
