"""Deterministic case scoring and run-level aggregation (pure functions, no I/O).

Every metric is computed by code from what the execution actually did (status, output,
tool-call trail, metered cost, step records). Nothing here asks a model to grade a model.
``groundedness`` is a *lexical* proxy (share of answer sentences whose content words are
mostly found in the retrieved sources) and is labelled as such everywhere it is reported.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from solutionforge.retrieval.embeddings import tokenize

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
GROUNDED_SENTENCE_OVERLAP = 0.6


class CaseExpectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "failed", "waiting", "budget_exceeded"] = "succeeded"
    output_subset: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    expected_tools: list[str] = Field(default_factory=list, max_length=20)
    forbidden_tools: list[str] = Field(default_factory=list, max_length=20)
    expected_cited_titles: list[str] = Field(default_factory=list, max_length=20)
    max_steps: int | None = Field(default=None, ge=1)
    max_cost_usd: Decimal | None = Field(default=None, ge=0)

    @field_validator("output_schema")
    @classmethod
    def _schema(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None:
            try:
                Draft202012Validator.check_schema(v)
            except SchemaError as exc:
                raise ValueError(f"invalid output_schema: {exc.message}") from None
        return v


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    answer: str
    insufficient_context: bool
    cited_titles: list[str]
    cited_chunk_ids: list[str]


@dataclass(frozen=True, slots=True)
class Observation:
    status: str
    output: dict[str, Any] | None
    error_code: str | None
    steps_used: int
    active_ms: int
    cost_micro_usd: int
    tokens: int
    tools_attempted: list[str]
    answers: list[GroundedAnswer] = field(default_factory=list)
    retrieved_chunk_ids: set[str] = field(default_factory=set)
    source_texts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CaseScore:
    passed: bool
    scores: dict[str, Any]
    failures: list[str]


def deep_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and deep_subset(v, actual[k]) for k, v in expected.items()
        )
    return bool(expected == actual)


def groundedness(answer: str, sources: list[str]) -> float | None:
    source_tokens = {t for s in sources for t in tokenize(s)}
    sentences = [s for s in _SENTENCE.split(answer) if len(tokenize(s)) >= 3]
    if not sentences:
        return None
    grounded = 0
    for s in sentences:
        toks = set(tokenize(s))
        if toks and len(toks & source_tokens) / len(toks) >= GROUNDED_SENTENCE_OVERLAP:
            grounded += 1
    return grounded / len(sentences)


def score_case(exp: CaseExpectations, obs: Observation) -> CaseScore:
    checks: dict[str, bool] = {}
    failures: list[str] = []

    def check(name: str, ok: bool, why: str) -> None:
        checks[name] = ok
        if not ok:
            failures.append(why)

    check("status", obs.status == exp.status, f"status {obs.status} != expected {exp.status}")
    if exp.output_subset is not None:
        check(
            "output_match",
            deep_subset(exp.output_subset, obs.output or {}),
            "output does not contain the expected values",
        )
    if exp.output_schema is not None:
        valid = obs.output is not None and Draft202012Validator(exp.output_schema).is_valid(
            obs.output
        )
        check("structured_output_valid", valid, "output does not match the expected schema")
    used = set(obs.tools_attempted)
    if exp.expected_tools or exp.forbidden_tools:
        missing = sorted(set(exp.expected_tools) - used)
        forbidden = sorted(set(exp.forbidden_tools) & used)
        check(
            "tool_selection",
            not missing and not forbidden,
            f"tool selection: missing={missing} forbidden_used={forbidden}",
        )
    if exp.expected_cited_titles or obs.answers:
        cited = {t for a in obs.answers for t in a.cited_titles}
        ids = {c for a in obs.answers for c in a.cited_chunk_ids}
        valid_refs = ids <= obs.retrieved_chunk_ids
        covers = set(exp.expected_cited_titles) <= cited
        check(
            "citations",
            valid_refs and covers,
            "citations reference unretrieved chunks"
            if not valid_refs
            else f"expected citations missing: {sorted(set(exp.expected_cited_titles) - cited)}",
        )
    if exp.max_steps is not None:
        check(
            "step_budget",
            obs.steps_used <= exp.max_steps,
            f"used {obs.steps_used} steps > {exp.max_steps}",
        )
    if exp.max_cost_usd is not None:
        cost = Decimal(obs.cost_micro_usd) / Decimal(1_000_000)
        check("cost_budget", cost <= exp.max_cost_usd, f"cost {cost} > {exp.max_cost_usd}")

    g_values = [
        g
        for a in obs.answers
        if not a.insufficient_context
        if (g := groundedness(a.answer, obs.source_texts)) is not None
    ]
    scores: dict[str, Any] = {
        **checks,
        "groundedness": round(sum(g_values) / len(g_values), 4) if g_values else None,
        "latency_ms": obs.active_ms,
        "cost_micro_usd": obs.cost_micro_usd,
        "tokens": obs.tokens,
        "steps": obs.steps_used,
        "error_code": obs.error_code,
    }
    return CaseScore(passed=all(checks.values()), scores=scores, failures=failures)


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (no interpolation): an actually observed value."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _rate(results: list[tuple[CaseScore, list[str]]], key: str) -> float | None:
    vals = [s.scores[key] for s, _ in results if isinstance(s.scores.get(key), bool)]
    return round(sum(vals) / len(vals), 4) if vals else None


def aggregate(results: list[tuple[CaseScore, list[str]]]) -> dict[str, Any]:
    """``results``: (score, case tags). Metrics are None when no case measures them."""
    n = len(results)
    if n == 0:
        return {"cases": 0}
    latencies = [float(s.scores["latency_ms"]) for s, _ in results]
    costs = [s.scores["cost_micro_usd"] for s, _ in results]
    g = [s.scores["groundedness"] for s, _ in results if s.scores.get("groundedness") is not None]
    security = [s.passed for s, tags in results if "security" in tags]
    errored = sum(1 for s, _ in results if s.scores.get("error_code"))
    return {
        "cases": n,
        "passed": sum(1 for s, _ in results if s.passed),
        "pass_rate": round(sum(1 for s, _ in results if s.passed) / n, 4),
        "tool_selection_accuracy": _rate(results, "tool_selection"),
        "structured_output_validity": _rate(results, "structured_output_valid"),
        "citation_accuracy": _rate(results, "citations"),
        "groundedness_lexical": round(sum(g) / len(g), 4) if g else None,
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "cost_per_case_usd": str(
            (Decimal(sum(costs)) / n / Decimal(1_000_000)).quantize(Decimal("0.000001"))
        ),
        "steps_mean": round(sum(s.scores["steps"] for s, _ in results) / n, 2),
        "error_rate": round(errored / n, 4),
        "security_cases": len(security),
        "security_cases_passed": sum(security),
    }
