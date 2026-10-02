"""Pure scoring, aggregation, and gate logic."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from solutionforge.evaluation.gate import GatePolicy, evaluate_gate
from solutionforge.evaluation.scorers import (
    CaseExpectations,
    CaseScore,
    GroundedAnswer,
    Observation,
    aggregate,
    deep_subset,
    groundedness,
    percentile,
    score_case,
)


def obs(**kw: Any) -> Observation:
    base: dict[str, Any] = {
        "status": "succeeded",
        "output": {"a": 1},
        "error_code": None,
        "steps_used": 2,
        "active_ms": 10,
        "cost_micro_usd": 0,
        "tokens": 0,
        "tools_attempted": [],
    }
    return Observation(**{**base, **kw})


def test_deep_subset() -> None:
    assert deep_subset({"a": {"b": 1}}, {"a": {"b": 1, "c": 2}, "d": 3})
    assert not deep_subset({"a": {"b": 1}}, {"a": {"b": 2}})
    assert not deep_subset({"a": 1}, {})
    assert not deep_subset({"a": {"b": 1}}, {"a": 5})
    assert deep_subset([1, 2], [1, 2]) and not deep_subset([1], [1, 2])


def test_score_case_checks_each_expectation() -> None:
    exp = CaseExpectations(
        output_subset={"a": 1},
        output_schema={"type": "object", "required": ["a"]},
        expected_tools=["crm.get_customer"],
        forbidden_tools=["payments.issue_refund"],
        max_steps=3,
        max_cost_usd=Decimal("0.001"),
    )
    ok = score_case(exp, obs(tools_attempted=["crm.get_customer"], cost_micro_usd=999))
    assert ok.passed and ok.failures == []
    bad = score_case(
        exp,
        obs(
            status="failed",
            output=None,
            tools_attempted=["payments.issue_refund"],
            steps_used=4,
            cost_micro_usd=1001,
            error_code="boom",
        ),
    )
    assert not bad.passed
    assert {k for k, v in bad.scores.items() if v is False} == {
        "status",
        "output_match",
        "structured_output_valid",
        "tool_selection",
        "step_budget",
        "cost_budget",
    }
    assert "forbidden_used=['payments.issue_refund']" in " ".join(bad.failures)


def test_citations_must_reference_retrieved_chunks() -> None:
    ans = GroundedAnswer("Refunds take 5 days.", False, ["Refund policy"], ["c1"])
    exp = CaseExpectations(expected_cited_titles=["Refund policy"])
    assert score_case(exp, obs(answers=[ans], retrieved_chunk_ids={"c1"})).passed
    s = score_case(exp, obs(answers=[ans], retrieved_chunk_ids={"c2"}))
    assert s.failures == ["citations reference unretrieved chunks"]


def test_groundedness_is_a_lexical_share_of_sentences() -> None:
    src = ["Refunds appear within five to ten business days after approval."]
    assert groundedness("Refunds appear within five business days.", src) == 1.0
    mixed = "Refunds appear within ten business days. Our CEO enjoys sailing yachts often."
    assert groundedness(mixed, src) == 0.5
    assert groundedness("Yes.", src) is None  # too short to judge
    s = score_case(
        CaseExpectations(),
        obs(answers=[GroundedAnswer(mixed, False, [], [])], source_texts=src),
    )
    assert s.scores["groundedness"] == 0.5
    abstain = GroundedAnswer("I don't know based on the sources given.", True, [], [])
    assert score_case(CaseExpectations(), obs(answers=[abstain])).scores["groundedness"] is None


def test_expectations_reject_unknown_keys_and_bad_schemas() -> None:
    with pytest.raises(ValidationError):
        CaseExpectations.model_validate({"llm_judge": True})
    with pytest.raises(Exception, match="not-a-type"):
        CaseExpectations.model_validate({"output_schema": {"type": "not-a-type"}})


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 95) is None
    assert percentile([5.0], 50) == 5.0
    vals = [float(v) for v in range(1, 21)]
    assert percentile(vals, 50) == 10.0 and percentile(vals, 95) == 19.0


@settings(suppress_health_check=[HealthCheck.too_slow])  # float lists; slow on busy CI hosts
@given(st.lists(st.floats(min_value=0, max_value=1e6), min_size=1, max_size=50))
def test_percentile_returns_an_observed_value(values: list[float]) -> None:
    for p in (1, 50, 95, 100):
        assert percentile(values, p) in values


def test_aggregate_reports_unmeasured_metrics_as_none() -> None:
    assert aggregate([]) == {"cases": 0}
    plain = CaseScore(
        True, {"status": True, "latency_ms": 10, "cost_micro_usd": 1500, "steps": 2}, []
    )
    sec = CaseScore(
        False,
        {"status": False, "latency_ms": 30, "cost_micro_usd": 500, "steps": 1, "error_code": "x"},
        ["status"],
    )
    m = aggregate([(plain, []), (sec, ["security"])])
    assert m["pass_rate"] == 0.5 and m["passed"] == 1
    assert m["citation_accuracy"] is None and m["tool_selection_accuracy"] is None
    assert m["cost_per_case_usd"] == "0.001000"
    assert m["latency_p50_ms"] == 10.0 and m["latency_p95_ms"] == 30.0
    assert m["security_cases"] == 1 and m["security_cases_passed"] == 0
    assert m["error_rate"] == 0.5 and m["steps_mean"] == 1.5


def _policy(**kw: Any) -> GatePolicy:
    return GatePolicy(dataset_id=uuid.uuid4(), **kw)


def test_gate_checks() -> None:
    cand = {
        "pass_rate": 0.9,
        "citation_accuracy": None,
        "tool_selection_accuracy": 1.0,
        "latency_p95_ms": 900.0,
        "cost_per_case_usd": "0.002000",
        "security_cases": 2,
        "security_cases_passed": 2,
    }
    policy = _policy(
        min_pass_rate=0.8,
        min_citation_accuracy=0.9,
        min_tool_selection_accuracy=0.9,
        max_p95_latency_ms=1000,
        max_cost_per_case_usd=Decimal("0.001"),
    )
    checks = {c["name"]: c for c in evaluate_gate(policy, cand, {"pass_rate": 0.95})}
    assert {n for n, c in checks.items() if not c["passed"]} == {
        "no_pass_rate_regression",
        "min_citation_accuracy",  # not measured counts as failing a required minimum
        "max_cost_per_case_usd",
    }
    assert checks["min_citation_accuracy"]["detail"] == "not measured by dataset"
    first = evaluate_gate(_policy(), cand, None)
    assert [c["name"] for c in first] == ["no_pass_rate_regression", "security_cases_pass"]
    assert all(c["passed"] for c in first)
    off = _policy(no_pass_rate_regression=False, require_security_cases_pass=False)
    assert evaluate_gate(off, cand, {"pass_rate": 1.0}) == []


def test_gate_policy_rejects_out_of_range_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _policy(min_pass_rate=1.5)
    with pytest.raises(ValidationError):
        GatePolicy.model_validate({"dataset_id": str(uuid.uuid4()), "skip": True})
