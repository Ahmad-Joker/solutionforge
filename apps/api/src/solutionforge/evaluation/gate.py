"""Deployment quality gate: compare a candidate version's evaluation against production."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GatePolicy(BaseModel):
    """Per-workflow rules a version must pass before it can become production."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: uuid.UUID
    no_pass_rate_regression: bool = True
    min_pass_rate: float | None = Field(default=None, ge=0, le=1)
    min_citation_accuracy: float | None = Field(default=None, ge=0, le=1)
    min_tool_selection_accuracy: float | None = Field(default=None, ge=0, le=1)
    max_p95_latency_ms: int | None = Field(default=None, ge=1)
    max_cost_per_case_usd: Decimal | None = Field(default=None, ge=0)
    require_security_cases_pass: bool = True


def check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def evaluate_gate(
    policy: GatePolicy, candidate: dict[str, Any], baseline: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Return every check (passed or not), so the decision record explains itself."""
    checks: list[dict[str, Any]] = []
    pr = candidate.get("pass_rate")
    if policy.no_pass_rate_regression:
        if baseline is None:
            checks.append(check("no_pass_rate_regression", True, "no production baseline yet"))
        else:
            base = baseline.get("pass_rate") or 0.0
            checks.append(
                check(
                    "no_pass_rate_regression",
                    (pr or 0.0) >= base,
                    f"candidate {pr} vs production {base}",
                )
            )
    if policy.min_pass_rate is not None:
        checks.append(
            check(
                "min_pass_rate",
                (pr or 0.0) >= policy.min_pass_rate,
                f"{pr} >= {policy.min_pass_rate}",
            )
        )
    for metric, threshold in (
        ("citation_accuracy", policy.min_citation_accuracy),
        ("tool_selection_accuracy", policy.min_tool_selection_accuracy),
    ):
        if threshold is not None:
            value = candidate.get(metric)
            ok = value is not None and value >= threshold
            checks.append(
                check(
                    f"min_{metric}",
                    ok,
                    f"{value} >= {threshold}" if value is not None else "not measured by dataset",
                )
            )
    if policy.max_p95_latency_ms is not None:
        p95 = candidate.get("latency_p95_ms")
        checks.append(
            check(
                "max_p95_latency_ms",
                p95 is not None and p95 <= policy.max_p95_latency_ms,
                f"{p95} <= {policy.max_p95_latency_ms}",
            )
        )
    if policy.max_cost_per_case_usd is not None:
        cost = Decimal(str(candidate.get("cost_per_case_usd", "0")))
        checks.append(
            check(
                "max_cost_per_case_usd",
                cost <= policy.max_cost_per_case_usd,
                f"{cost} <= {policy.max_cost_per_case_usd}",
            )
        )
    if policy.require_security_cases_pass:
        total, ok = candidate.get("security_cases", 0), candidate.get("security_cases_passed", 0)
        checks.append(
            check("security_cases_pass", ok == total, f"{ok}/{total} security cases passed")
        )
    return checks
