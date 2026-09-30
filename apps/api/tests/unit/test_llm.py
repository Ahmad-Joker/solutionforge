"""LLM layer without a database: pricing, structured output, breaker, service semantics."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from solutionforge.llm import structured
from solutionforge.llm.breaker import BreakerState, CircuitBreaker
from solutionforge.llm.pricing import ModelPrice, PriceTable, micro_to_usd, usd_to_micro
from solutionforge.llm.providers.mock import MockProvider, MockReply, minimal_instance
from solutionforge.llm.service import LLMService, OrgLimits, RetryConfig, Spend, UsageEntry
from solutionforge.llm.types import (
    AllModelsFailed,
    BudgetExceeded,
    CallContext,
    InvalidRequest,
    LLMCall,
    Message,
    ModelRef,
    Usage,
)

ORG = uuid.uuid4()
EXEC = uuid.uuid4()
M1, M2 = ModelRef("mock", "mock-1"), ModelRef("mock", "mock-fast")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["refund", "question"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["intent", "confidence", "tags"],
    "additionalProperties": False,
}


class MemoryLedger:
    def __init__(self, limits: OrgLimits | None = None) -> None:
        self.entries: list[UsageEntry] = []
        self._limits = limits or OrgLimits()

    async def record(self, entry: UsageEntry) -> None:
        self.entries.append(entry)

    async def limits(self, organization_id: uuid.UUID) -> OrgLimits:
        return self._limits

    async def spent(self, ctx: CallContext) -> Spend:
        mine = [e for e in self.entries if e.organization_id == ctx.organization_id]
        ex = [e for e in mine if ctx.execution_id and e.execution_id == ctx.execution_id]
        total = sum(e.cost_micro_usd for e in mine)
        return Spend(
            total,
            total,
            sum(e.cost_micro_usd for e in ex),
            sum(e.usage.total_tokens for e in ex),
        )


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make(
    limits: OrgLimits | None = None, **kw: Any
) -> tuple[LLMService, MockProvider, MemoryLedger]:
    mock, ledger = MockProvider(), MemoryLedger(limits)
    svc = LLMService(
        providers={"mock": mock},
        prices=PriceTable(),
        ledger=ledger,
        sleep=FakeSleep(),
        retry=RetryConfig(backoff_base_seconds=0.5),
        **kw,
    )
    return svc, mock, ledger


def call(**kw: Any) -> LLMCall:
    base: dict[str, Any] = {
        "models": (M1,),
        "messages": (Message("user", "classify: I want my money back"),),
    }
    base.update(kw)
    return LLMCall(**base)


CTX = CallContext(organization_id=ORG, execution_id=EXEC, step_id="s")


# ------------------------------------------------------------------ pricing


def test_cost_is_exact_integer_micro_usd_rounded_up() -> None:
    p = ModelPrice.of("5.00", "25.00", "0.50", "6.25")
    assert p.cost_micro_usd(Usage(input_tokens=1_000_000)) == 5_000_000
    assert p.cost_micro_usd(Usage(input_tokens=1000, output_tokens=200)) == 5000 + 5000
    assert p.cost_micro_usd(Usage(cache_read_tokens=1)) == 1  # 0.5 micro-USD rounds up
    assert p.cost_micro_usd(Usage()) == 0


def test_usd_conversions_round_trip() -> None:
    assert usd_to_micro(Decimal("1.25")) == 1_250_000
    assert micro_to_usd(1_250_001) == Decimal("1.250001")


def test_price_overrides_and_unknown_models() -> None:
    t = PriceTable().with_overrides(
        {"mock:mock-1": {"input": "9", "output": "9", "cache_read": "0", "cache_write": "0"}}
    )
    assert t.get(M1).input == Decimal(9)  # type: ignore[union-attr]
    assert t.get(ModelRef("mock", "nope")) is None
    with pytest.raises(ValueError, match="provider:model"):
        PriceTable().with_overrides(
            {"bad": {"input": "1", "output": "1", "cache_read": "0", "cache_write": "0"}}
        )


# ------------------------------------------------------------------ structured output


def test_minimal_instance_satisfies_schema() -> None:
    assert Draft202012Validator(SCHEMA).is_valid(minimal_instance(SCHEMA))


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ('{"intent":"refund","confidence":0.9,"tags":["a"]}', True),
        ('```json\n{"intent":"refund","confidence":0.9,"tags":["a"]}\n```', True),
        ('{"intent":"refund"', False),
        ('{"intent":"steal","confidence":2,"tags":[]}', False),
        ("Sure! Here is the JSON: {}", False),
    ],
)
def test_parse_and_validate(text: str, ok: bool) -> None:
    value, errors = structured.parse_and_validate(text, SCHEMA)
    assert (value is not None) == ok and (not errors) == ok


@pytest.mark.parametrize(
    "schema", [{"type": "array"}, {"type": "object", "properties": {"x": {"type": "nope"}}}]
)
def test_check_schema_rejects_bad_schemas(schema: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="schema"):
        structured.check_schema(schema)


# ------------------------------------------------------------------ circuit breaker


def test_breaker_lifecycle() -> None:
    now = [0.0]
    b = CircuitBreaker(failure_threshold=3, recovery_seconds=10, clock=lambda: now[0])
    for _ in range(3):
        assert b.allow("m")
        b.record_failure("m")
    assert b.state("m") == BreakerState.OPEN and not b.allow("m")
    now[0] = 10
    assert b.allow("m")  # half-open: one trial
    assert not b.allow("m")  # ...only one
    b.record_failure("m")  # trial failed -> open again
    assert b.state("m") == BreakerState.OPEN
    now[0] = 25
    assert b.allow("m")
    b.record_success("m")
    assert b.state("m") == BreakerState.CLOSED


# ------------------------------------------------------------------ service


async def test_text_call_is_metered() -> None:
    svc, _, ledger = make()
    r = await svc.generate(call(), CTX)
    assert r.text.startswith("[mock-1:") and r.json is None
    assert r.calls == 1 and not r.fallback_used
    [e] = ledger.entries
    assert (e.outcome, e.execution_id, e.step_id, e.model) == ("ok", EXEC, "s", "mock-1")
    assert e.cost_micro_usd == PriceTable().get(M1).cost_micro_usd(e.usage) > 0  # type: ignore[union-attr]
    assert r.cost_micro_usd == e.cost_micro_usd


async def test_schema_call_returns_validated_json() -> None:
    svc, mock, _ = make()
    r = await svc.generate(call(output_schema=SCHEMA), CTX)
    assert r.json == minimal_instance(SCHEMA)
    assert mock.calls[0].output_schema == SCHEMA  # passed natively


async def test_non_native_provider_gets_schema_as_instruction_and_is_still_validated() -> None:
    svc, mock, _ = make(native_structured_output=frozenset())
    mock.script(MockReply.json({"intent": "question", "confidence": 0.5, "tags": ["t"]}))
    r = await svc.generate(call(output_schema=SCHEMA, system="Be precise."), CTX)
    assert r.json["intent"] == "question"
    sent = mock.calls[0]
    assert sent.output_schema is None
    assert (sent.system or "").startswith("Be precise.") and "JSON Schema" in (sent.system or "")


async def test_invalid_json_is_repaired() -> None:
    svc, mock, ledger = make()
    mock.script(
        MockReply.raw("definitely not json"),
        MockReply.json({"intent": "refund", "confidence": 1, "tags": ["x"]}),
    )
    r = await svc.generate(call(output_schema=SCHEMA, max_repairs=1), CTX)
    assert r.json["intent"] == "refund" and r.calls == 2
    assert [e.outcome for e in ledger.entries] == ["output_invalid", "ok"]
    repair_turn = mock.calls[1].messages
    assert [m["role"] for m in repair_turn] == ["user", "assistant", "user"]
    assert "not valid JSON" in repair_turn[-1]["content"]


async def test_exhausted_repairs_fall_back_to_next_model() -> None:
    svc, mock, _ = make()
    mock.script(MockReply.raw("{}"), MockReply.raw("{}"), model="mock-1")
    r = await svc.generate(call(models=(M1, M2), output_schema=SCHEMA, max_repairs=1), CTX)
    assert r.model == M2 and r.fallback_used and r.calls == 3


async def test_transient_errors_retry_with_exponential_backoff() -> None:
    svc, mock, ledger = make()
    mock.script(MockReply.error("rate_limit"), MockReply.error("unavailable"), MockReply.text("ok"))
    r = await svc.generate(call(attempts_per_model=3), CTX)
    assert r.text == "ok" and r.calls == 3
    delays = svc.sleep.delays  # type: ignore[attr-defined]
    assert len(delays) == 2 and 0.45 <= delays[0] <= 0.55 and 0.9 <= delays[1] <= 1.1
    assert [e.outcome for e in ledger.entries] == ["rate_limited", "provider_unavailable", "ok"]


async def test_fallback_after_retries_exhausted() -> None:
    svc, mock, _ = make()
    mock.script(MockReply.error("unavailable"), MockReply.error("unavailable"), model="mock-1")
    r = await svc.generate(call(models=(M1, M2), attempts_per_model=2), CTX)
    assert r.model == M2 and r.fallback_used


async def test_all_transient_failures_are_retryable() -> None:
    svc, mock, _ = make()
    mock.script(*[MockReply.error("unavailable")] * 4)
    with pytest.raises(AllModelsFailed) as ei:
        await svc.generate(call(models=(M1, M2), attempts_per_model=2), CTX)
    assert ei.value.retryable


@pytest.mark.parametrize("kind", ["refusal", "truncated"])
async def test_refusal_and_truncation_fall_back_without_retry(kind: str) -> None:
    svc, mock, _ = make()
    mock.script(MockReply.error(kind), model="mock-1")  # type: ignore[arg-type]
    r = await svc.generate(call(models=(M1, M2)), CTX)
    assert r.model == M2 and r.calls == 2


async def test_non_transient_chain_failure_is_not_retryable() -> None:
    svc, mock, _ = make()
    mock.script(MockReply.error("refusal"), MockReply.error("refusal"))
    with pytest.raises(AllModelsFailed) as ei:
        await svc.generate(call(models=(M1, M2)), CTX)
    assert not ei.value.retryable
    assert [e.code for e in ei.value.errors] == ["refused", "refused"]


async def test_invalid_request_stops_immediately() -> None:
    svc, mock, _ = make()
    mock.script(MockReply.error("bad_request"))
    with pytest.raises(InvalidRequest):
        await svc.generate(call(models=(M1, M2)), CTX)
    assert len(mock.calls) == 1  # no retry, no fallback


async def test_call_timeout_is_enforced_and_retried() -> None:
    svc, mock, ledger = make()
    mock.script(MockReply.slow(5), MockReply.text("fast"))
    r = await svc.generate(call(timeout_seconds=0.05, attempts_per_model=2), CTX)
    assert r.text == "fast"
    assert ledger.entries[0].outcome == "provider_unavailable"


async def test_open_circuit_skips_model_without_calling_it() -> None:
    svc, mock, _ = make(breaker=CircuitBreaker(failure_threshold=2, recovery_seconds=60))
    mock.script(MockReply.error("unavailable"), MockReply.error("unavailable"), model="mock-1")
    await svc.generate(call(models=(M1, M2), attempts_per_model=2), CTX)  # trips the breaker
    before = len([c for c in mock.calls if c.model == "mock-1"])
    r = await svc.generate(call(models=(M1, M2)), CTX)
    assert r.model == M2
    assert len([c for c in mock.calls if c.model == "mock-1"]) == before


async def test_unpriced_model_is_refused() -> None:
    svc, _, _ = make()
    with pytest.raises(InvalidRequest, match="no price"):
        await svc.generate(call(models=(ModelRef("mock", "gpt-free"),)), CTX)


async def test_unconfigured_provider_falls_back() -> None:
    svc, _, _ = make()
    svc.prices = PriceTable().with_overrides(
        {"other:x": {"input": "1", "output": "1", "cache_read": "0", "cache_write": "0"}}
    )
    r = await svc.generate(call(models=(ModelRef("other", "x"), M1)), CTX)
    assert r.model == M1


# ------------------------------------------------------------------ budgets


async def test_budget_precheck_blocks_before_calling_provider() -> None:
    svc, mock, ledger = make(limits=OrgLimits(daily_micro_usd=100))
    with pytest.raises(BudgetExceeded) as ei:
        await svc.generate(call(max_tokens=1000), CTX)  # worst case 2000+ micro-USD
    assert ei.value.details["scope"] == "daily"
    assert mock.calls == [] and ledger.entries == []


async def test_spend_accumulates_until_budget_stops_further_calls() -> None:
    svc, _, _ = make(limits=OrgLimits(monthly_micro_usd=3_000))
    small = call(max_tokens=500)  # worst case ~1,010 micro-USD; actual much less
    n = 0
    with pytest.raises(BudgetExceeded):  # noqa: PT012 - loop until the budget stops it
        for _ in range(500):
            await svc.generate(small, CTX)
            n += 1
    assert 1 < n < 500  # stopped by budget, not by the loop bound
    # Pre-check with worst-case estimate guarantees committed spend never exceeds the limit.
    assert sum(e.cost_micro_usd for e in svc.ledger.entries) <= 3_000  # type: ignore[attr-defined]


async def test_execution_limits_from_context() -> None:
    svc, _, _ = make(limits=OrgLimits(per_execution_micro_usd=10_000_000))
    tight = CallContext(organization_id=ORG, execution_id=EXEC, execution_cost_limit_micro_usd=50)
    with pytest.raises(BudgetExceeded) as ei:
        await svc.generate(call(), tight)
    assert ei.value.details["scope"] == "per_execution"  # min(org, workflow) applies

    tokens = CallContext(organization_id=ORG, execution_id=EXEC, execution_token_limit=100)
    with pytest.raises(BudgetExceeded, match="token"):
        await svc.generate(call(max_tokens=500), tokens)


# ------------------------------------------------------------------ architecture


def test_vendor_sdk_imported_only_in_its_adapter() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "solutionforge"
    allowed = {src / "llm" / "providers" / "claude.py"}
    offenders = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if p not in allowed
        and any(
            line.strip().startswith(
                ("import anthropic", "from anthropic", "import openai", "from openai")
            )
            for line in p.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert offenders == []
