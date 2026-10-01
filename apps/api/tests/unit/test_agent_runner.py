"""Agent loop semantics with a scripted model and a fake executor (no DB)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from solutionforge.agents.runner import (
    MAX_OBSERVATION_CHARS,
    AgentConfig,
    AgentStopped,
    action_schema,
    run_agent,
)
from solutionforge.llm.pricing import PriceTable
from solutionforge.llm.providers.mock import MockProvider, MockReply
from solutionforge.llm.service import LLMService
from solutionforge.llm.types import CallContext, ModelRef
from solutionforge.tools.catalog import default_catalog
from solutionforge.tools.executor import ToolInvocation
from solutionforge.tools.spec import ToolApprovalRequired, ToolError, ToolInputInvalid
from tests.unit.test_llm import MemoryLedger

ORG = uuid.uuid4()
CTX = CallContext(organization_id=ORG, execution_id=uuid.uuid4(), step_id="agent")
TOOLS = ("crm.get_customer", "ticketing.create_ticket", "email.send_message")


class FakeExecutor:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.catalog = default_catalog()
        self.calls: list[dict[str, Any]] = []
        self.responses = responses or {}

    async def invoke(self, **kw: Any) -> ToolInvocation:
        self.calls.append(kw)
        r = self.responses.get(kw["tool_name"], {"ok": True})
        if isinstance(r, ToolError):
            raise r
        return ToolInvocation(uuid.uuid4(), r, replayed=False, attempts=1)


def setup(*replies: MockReply, **cfg: Any) -> tuple[AgentConfig, LLMService, MockProvider]:
    mock = MockProvider()
    mock.script(*replies)
    svc = LLMService(providers={"mock": mock}, prices=PriceTable(), ledger=MemoryLedger())
    base: dict[str, Any] = {
        "models": (ModelRef("mock", "mock-1"),),
        "task": "Help C-1001",
        "tools": TOOLS,
    }
    base.update(cfg)
    return AgentConfig(**base), svc, mock


def call(tool: str, args: dict[str, Any], reason: str = "need data") -> MockReply:
    return MockReply.json({"action": "call_tool", "tool": tool, "args": args, "reason": reason})


def final(answer: Any, reason: str = "done") -> MockReply:
    return MockReply.json({"action": "final", "answer": answer, "reason": reason})


async def test_tool_then_final_with_trace_and_observations() -> None:
    cfg, svc, mock = setup(
        call("crm.get_customer", {"customer_ref": "C-1001"}),
        final({"summary": "gold customer"}),
    )
    ex = FakeExecutor({"crm.get_customer": {"ref": "C-1001", "tier": "gold"}})
    r = await run_agent(cfg, llm=svc, executor=ex, ctx=CTX, key_prefix="e:agent:1")
    assert r.answer == {"summary": "gold customer"} and r.turns == 2 and r.tool_calls == 1
    assert [t["outcome"] for t in r.trace] == ["ok", "final"]
    assert r.trace[0]["tool"] == "crm.get_customer" and r.trace[0]["reason"] == "need data"
    # The model saw the tool result as delimited, untrusted data.
    second_turn = mock.calls[1].messages
    assert (
        second_turn[-1]["content"].startswith("<observation>")
        and '"tier":"gold"' in second_turn[-1]["content"]
    )
    assert "untrusted data" in (mock.calls[0].system or "")
    assert r.cost_micro_usd > 0


async def test_each_action_gets_a_distinct_stable_idempotency_key() -> None:
    cfg, svc, _ = setup(
        call("ticketing.create_ticket", {"subject": "a", "body": "b"}),
        call("ticketing.create_ticket", {"subject": "c", "body": "d"}),
        final("ok"),
    )
    ex = FakeExecutor()
    await run_agent(cfg, llm=svc, executor=ex, ctx=CTX, key_prefix="exec:agent:3")
    keys = [c["idempotency_key"] for c in ex.calls]
    assert len(set(keys)) == 2 and all(k.startswith("exec:agent:3:a") for k in keys)

    cfg2, svc2, _ = setup(
        call("ticketing.create_ticket", {"subject": "a", "body": "b"}), final("ok")
    )
    ex2 = FakeExecutor()
    await run_agent(cfg2, llm=svc2, executor=ex2, ctx=CTX, key_prefix="exec:agent:3")
    assert ex2.calls[0]["idempotency_key"] == keys[0]  # same decision on re-run → same key


async def test_disallowed_tool_is_rejected_by_the_action_schema() -> None:
    cfg, svc, mock = setup(
        call("payments.issue_refund", {"order_ref": "O-50001", "amount_cents": 1, "reason": "x"}),
        final("gave up"),
    )
    ex = FakeExecutor()
    r = await run_agent(cfg, llm=svc, executor=ex, ctx=CTX, key_prefix="k")
    assert ex.calls == [] and r.answer == "gave up"
    assert "payments.issue_refund" in mock.calls[1].messages[-1]["content"]  # repair feedback


async def test_disallowed_tool_is_refused_by_code_even_if_schema_allowed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: the allowlist check doesn't depend on schema enforcement."""
    from solutionforge.agents import runner

    monkeypatch.setattr(
        runner,
        "action_schema",
        lambda tools: {"type": "object", "properties": {}, "required": ["action", "reason"]},
    )
    cfg, svc, _ = setup(
        call("payments.issue_refund", {"order_ref": "O-50001", "amount_cents": 1, "reason": "x"}),
        final("gave up"),
    )
    ex = FakeExecutor()
    r = await run_agent(cfg, llm=svc, executor=ex, ctx=CTX, key_prefix="k")
    assert ex.calls == []
    assert r.trace[0]["outcome"] == "tool_not_allowed"


async def test_invalid_args_and_policy_blocks_become_observations() -> None:
    cfg, svc, mock = setup(
        call("crm.get_customer", {"customer_ref": "bogus"}),
        call("email.send_message", {"message_id": str(uuid.uuid4())}),
        final("escalated to a human"),
    )
    ex = FakeExecutor(
        {
            "crm.get_customer": ToolInputInvalid(
                "invalid arguments", errors=[{"loc": ["customer_ref"], "msg": "bad"}]
            ),
            "email.send_message": ToolApprovalRequired("requires approval: external action"),
        }
    )
    r = await run_agent(cfg, llm=svc, executor=ex, ctx=CTX, key_prefix="k")
    assert [t["outcome"] for t in r.trace] == ["tool_error", "blocked_by_policy", "final"]
    assert "tool_approval_required" in mock.calls[2].messages[-1]["content"]


async def test_final_answer_schema_feedback() -> None:
    schema = {
        "type": "object",
        "properties": {"priority": {"enum": ["low", "high"]}},
        "required": ["priority"],
        "additionalProperties": False,
    }
    cfg, svc, mock = setup(
        final({"priority": "urgent"}), final({"priority": "high"}), output_schema=schema
    )
    r = await run_agent(cfg, llm=svc, executor=FakeExecutor(), ctx=CTX, key_prefix="k")
    assert r.answer == {"priority": "high"} and r.turns == 2
    assert r.trace[0]["outcome"] == "answer_invalid"
    assert "does not match the schema" in mock.calls[1].messages[-1]["content"]


async def test_max_turns() -> None:
    cfg, svc, _ = setup(
        *[call("crm.get_customer", {"customer_ref": f"C-100{i}"}) for i in range(5)], max_turns=3
    )
    with pytest.raises(AgentStopped) as ei:
        await run_agent(cfg, llm=svc, executor=FakeExecutor(), ctx=CTX, key_prefix="k")
    assert ei.value.code == "agent_max_turns" and len(ei.value.trace) == 3


async def test_tool_call_budget_forces_final() -> None:
    cfg, svc, _ = setup(
        call("crm.get_customer", {"customer_ref": "C-1001"}),
        call("crm.get_customer", {"customer_ref": "C-1002"}),
        final("partial"),
        max_tool_calls=1,
    )
    ex = FakeExecutor()
    r = await run_agent(cfg, llm=svc, executor=ex, ctx=CTX, key_prefix="k")
    assert len(ex.calls) == 1 and r.answer == "partial"
    assert r.trace[1]["outcome"] == "tool_budget_exhausted"


async def test_repeated_identical_calls_are_detected() -> None:
    same = call("crm.get_customer", {"customer_ref": "C-1001"})
    cfg, svc, _ = setup(same, same, same, same)
    ex = FakeExecutor()
    with pytest.raises(AgentStopped) as ei:
        await run_agent(cfg, llm=svc, executor=ex, ctx=CTX, key_prefix="k")
    assert ei.value.code == "agent_loop_detected" and len(ex.calls) == 2


async def test_large_observations_are_truncated() -> None:
    cfg, svc, mock = setup(call("crm.get_customer", {"customer_ref": "C-1001"}), final("ok"))
    ex = FakeExecutor({"crm.get_customer": {"blob": "x" * 50_000}})
    await run_agent(cfg, llm=svc, executor=ex, ctx=CTX, key_prefix="k")
    obs = mock.calls[1].messages[-1]["content"]
    assert len(obs) < MAX_OBSERVATION_CHARS + 100 and "[truncated]" in obs


def test_action_schema_constrains_tools() -> None:
    s = action_schema(("a.b", "c.d"))
    assert s["properties"]["tool"]["enum"] == ["a.b", "c.d"]
    assert s["additionalProperties"] is False and "tool" not in action_schema(())["properties"]
