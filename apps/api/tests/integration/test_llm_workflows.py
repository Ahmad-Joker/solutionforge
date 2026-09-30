"""LLM steps inside real workflows, metering to the DB ledger, budgets and usage APIs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.domain import AuditEvent, UsageRecord
from solutionforge.llm.providers.mock import MockProvider, MockReply
from solutionforge.workflows.engine import Engine
from tests.helpers import Api
from tests.workflow_support import get_exec, make_workflow, start, step

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["refund", "question", "complaint"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["intent", "confidence"],
    "additionalProperties": False,
}


def triage_workflow(**limits: Any) -> dict[str, Any]:
    return {
        "start": "classify",
        "inputs": {"message": {"type": "string"}},
        "limits": limits,
        "steps": [
            step(
                "classify",
                "llm",
                {
                    "model": "mock:mock-1",
                    "fallback_models": ["mock:mock-fast"],
                    "system": "Classify customer support messages.",
                    "prompt": "Message: {{ $.input.message }}",
                    "output_schema": TRIAGE_SCHEMA,
                    "max_tokens": 200,
                },
                retry={"max_attempts": 2, "initial_backoff_seconds": 0},
                next="route",
            ),
            step(
                "route",
                "condition",
                {
                    "branches": [
                        {
                            "when": {
                                "all": [
                                    {
                                        "path": "$.steps.classify.json.intent",
                                        "op": "eq",
                                        "value": "refund",
                                    },
                                    {
                                        "path": "$.steps.classify.json.confidence",
                                        "op": "gte",
                                        "value": 0.8,
                                    },
                                ]
                            },
                            "goto": "auto_refund",
                        }
                    ],
                    "default": "escalate",
                },
            ),
            step("auto_refund", "transform", {"set": {"action": "refund"}}),
            step("escalate", "transform", {"set": {"action": "escalate"}}),
        ],
        "output": {"action": "$.state.action", "intent": "$.steps.classify.json.intent"},
    }


async def test_llm_structured_output_drives_branching_and_is_metered(
    api: Api, engine: Engine, mock_llm: MockProvider, db: AsyncSession
) -> None:
    mock_llm.script(MockReply.json({"intent": "refund", "confidence": 0.93}), match="money back")
    s = await make_workflow(api, triage_workflow())
    refund = await start(api, s, {"message": "I want my money back"})
    other = await start(api, s, {"message": "How do I reset my password?"})
    await engine.run_until_idle()

    ex = await get_exec(api, s, refund)
    assert ex["status"] == "succeeded"
    assert ex["output"] == {"action": "refund", "intent": "refund"}
    classify = ex["steps"][0]["output"]
    assert classify["model"] == "mock:mock-1" and classify["calls"] == 1
    assert Decimal(classify["usage"]["cost_usd"]) > 0
    # The prompt template was rendered with execution input, and the schema was passed natively.
    assert mock_llm.calls[0].messages[-1]["content"] == "Message: I want my money back"
    assert mock_llm.calls[0].output_schema == TRIAGE_SCHEMA

    # Default mock output (first enum value, minimum confidence) → escalates.
    assert (await get_exec(api, s, other))["output"]["action"] == "escalate"

    rows = (await db.scalars(sa.select(UsageRecord).order_by(UsageRecord.created_at))).all()
    assert len(rows) == 2 and all(r.step_id == "classify" and r.outcome == "ok" for r in rows)
    assert {str(r.execution_id) for r in rows} == {refund, other}
    assert ex["llm_usage"]["calls"] == 1
    assert Decimal(ex["llm_usage"]["cost_usd"]) == Decimal(classify["usage"]["cost_usd"])


async def test_repair_and_fallback_are_visible_in_step_output(
    api: Api, engine: Engine, mock_llm: MockProvider
) -> None:
    mock_llm.script(MockReply.raw("nope"), MockReply.raw("still nope"), model="mock-1")
    s = await make_workflow(api, triage_workflow())
    eid = await start(api, s, {"message": "hello"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    out = ex["steps"][0]["output"]
    assert ex["status"] == "succeeded"
    assert out["fallback_used"] is True and out["model"] == "mock:mock-fast" and out["calls"] == 3
    assert ex["llm_usage"]["calls"] == 3  # failed attempts are billed and recorded too


async def test_transient_llm_outage_is_retried_by_the_engine(
    api: Api, engine: Engine, mock_llm: MockProvider, db: AsyncSession
) -> None:
    # Both models down for the whole first step attempt (2 attempts x 2 models), then healthy.
    mock_llm.script(*[MockReply.error("unavailable")] * 4)
    s = await make_workflow(api, triage_workflow())
    eid = await start(api, s, {"message": "hello"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded"
    assert [(st["attempt"], st["status"]) for st in ex["steps"][:2]] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    assert ex["steps"][0]["error"]["code"] == "llm_failed"
    assert ex["steps"][0]["error"]["retryable"] is True
    outcomes = (await db.scalars(sa.select(UsageRecord.outcome))).all()
    assert outcomes.count("provider_unavailable") == 4


async def test_persistently_invalid_output_fails_without_engine_retry(
    api: Api, engine: Engine, mock_llm: MockProvider
) -> None:
    mock_llm.script(*[MockReply.raw("not json")] * 10)
    s = await make_workflow(api, triage_workflow())
    eid = await start(api, s, {"message": "hello"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed"
    assert ex["error"]["code"] == "llm_failed" and ex["error"]["retryable"] is False
    assert len(ex["steps"]) == 1  # deterministic failure: no pointless step retry
    errs = ex["error"]["details"]["errors"]
    assert {e["code"] for e in errs} == {"output_invalid"} and errs[0]["validation_errors"]


# ------------------------------------------------------------------ budgets


async def test_workflow_cost_limit_stops_execution_as_budget_exceeded(
    api: Api, engine: Engine, mock_llm: MockProvider
) -> None:
    s = await make_workflow(api, triage_workflow(max_cost_usd="0.0001"))  # 100 micro-USD
    eid = await start(api, s, {"message": "hello"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "budget_exceeded"
    assert ex["error"]["code"] == "llm_budget_exceeded"
    assert ex["error"]["details"]["scope"] == "per_execution"
    assert mock_llm.calls == []  # blocked before spending anything


async def test_workflow_token_limit(api: Api, engine: Engine, mock_llm: MockProvider) -> None:
    s = await make_workflow(api, triage_workflow(max_llm_tokens=50))
    eid = await start(api, s, {"message": "hello"})
    await engine.run_until_idle()
    assert (await get_exec(api, s, eid))["status"] == "budget_exceeded"


async def test_org_daily_budget_via_api(
    api: Api, client: AsyncClient, engine: Engine, mock_llm: MockProvider, db: AsyncSession
) -> None:
    s = await make_workflow(api, triage_workflow())
    r = await client.put(
        s.url("/budget"), json={"daily_limit_usd": "0.0002"}, headers=s.owner.headers
    )
    assert r.status_code == 200
    assert r.json()["daily_limit_usd"] == "0.000200" and r.json()["monthly_limit_usd"] is None
    eid = await start(api, s, {"message": "hello"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "budget_exceeded" and ex["error"]["details"]["scope"] == "daily"
    assert await db.scalar(
        sa.select(AuditEvent.id).where(AuditEvent.event_type == "budget.updated")
    )

    # Raising the budget lets new executions run.
    await client.put(s.url("/budget"), json={"daily_limit_usd": "5"}, headers=s.owner.headers)
    eid2 = await start(api, s, {"message": "hello"})
    await engine.run_until_idle()
    assert (await get_exec(api, s, eid2))["status"] == "succeeded"


async def test_budget_validation(api: Api, client: AsyncClient) -> None:
    s = await make_workflow(api, triage_workflow())
    for bad in ({"daily_limit_usd": "-1"}, {"daily_limit_usd": "0.0000001"}, {"weekly": "1"}):
        r = await client.put(s.url("/budget"), json=bad, headers=s.owner.headers)
        assert r.status_code == 422, bad


# ------------------------------------------------------------------ definition validation


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"model": "mock:does-not-exist"}, "unknown or unpriced model"),
        (
            {"model": "anthropic:claude-opus-5"},
            "unknown or unpriced model",
        ),  # provider not configured here
        ({"model": "no-provider"}, "provider:model"),
        ({"output_schema": {"type": "array"}}, "type: object"),
        (
            {"output_schema": {"type": "object", "properties": {"a": {"type": 7}}}},
            "invalid JSON Schema",
        ),
        ({"prompt": "{{ $.input.undeclared }}"}, "undeclared input"),
        ({"temperature": 0.7}, "Extra inputs"),
    ],
)
async def test_llm_step_config_is_validated_at_version_creation(
    api: Api, client: AsyncClient, patch: dict[str, Any], needle: str
) -> None:
    s = await make_workflow(api, triage_workflow())
    bad = triage_workflow()
    bad["steps"][0]["config"].update(patch)
    r = await client.post(
        s.url(f"/workflows/{s.workflow}/versions"),
        json={"definition": bad},
        headers=s.owner.headers,
    )
    assert r.status_code == 422
    assert needle in str(r.json()["error"]["details"])


# ------------------------------------------------------------------ usage reporting


async def test_usage_summary_and_records(
    api: Api, client: AsyncClient, engine: Engine, mock_llm: MockProvider
) -> None:
    mock_llm.script(MockReply.error("rate_limit"), model="mock-1")
    s = await make_workflow(api, triage_workflow())
    eid = await start(api, s, {"message": "hello"})
    await engine.run_until_idle()

    summary = (await client.get(s.url("/usage/summary"), headers=s.owner.headers)).json()
    [row] = summary
    assert row["model"] == "mock:mock-1" and row["calls"] == 2 and row["failed_calls"] == 1
    records = (
        await client.get(
            s.url("/usage/records"), params={"execution_id": eid}, headers=s.owner.headers
        )
    ).json()
    assert [r["outcome"] for r in records] == ["ok", "rate_limited"]  # newest first
    total = sum(Decimal(r["cost_usd"]) for r in records)
    assert Decimal(row["cost_usd"]) == total
