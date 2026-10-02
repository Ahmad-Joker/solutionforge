"""LLMService: the single entry point for model calls.

Per call, in order:

1. Every model in the chain must have a price (no unmetered models).
2. For each model (primary, then fallbacks):
   a. circuit breaker must allow it;
   b. **budget pre-check** with a worst-case estimate (input estimate + max_tokens);
   c. one provider attempt under a timeout;
   d. **meter** the attempt (tokens, cost, latency, outcome), success *or* failure;
   e. transient error → exponential backoff and retry (up to ``attempts_per_model``);
      refusal / truncation / exhausted retries / open circuit → next model;
      invalid request / budget exceeded → stop immediately (another model won't help);
   f. structured output → parse + JSON-Schema validate; on failure send a repair turn
      (up to ``max_repairs``), then move on.
3. All models failed → :class:`AllModelsFailed` (``retryable`` if every failure was transient).
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from solutionforge.core.logging import get_logger
from solutionforge.llm import structured
from solutionforge.llm.breaker import CircuitBreaker
from solutionforge.llm.pricing import PriceTable, estimate_tokens
from solutionforge.llm.types import (
    AllModelsFailed,
    BudgetExceeded,
    CallContext,
    CircuitOpen,
    InvalidRequest,
    LLMCall,
    LLMError,
    LLMProvider,
    LLMResult,
    Message,
    ModelRef,
    OutputInvalid,
    ProviderAuthError,
    ProviderRequest,
    ProviderUnavailable,
    RateLimited,
    Refused,
    Truncated,
    Usage,
)
from solutionforge.observability import metrics
from solutionforge.observability.tracing import tracer

log = get_logger(__name__)


# --------------------------------------------------------------------------- ledger contract


@dataclass(frozen=True, slots=True)
class UsageEntry:
    organization_id: uuid.UUID
    execution_id: uuid.UUID | None
    step_id: str | None
    purpose: str
    provider: str
    model: str
    outcome: str  # "ok" or an error code
    usage: Usage
    cost_micro_usd: int
    latency_ms: int
    attempt: int
    fallback_index: int
    request_id: str | None


@dataclass(frozen=True, slots=True)
class OrgLimits:
    daily_micro_usd: int | None = None
    monthly_micro_usd: int | None = None
    per_execution_micro_usd: int | None = None


@dataclass(frozen=True, slots=True)
class Spend:
    day_micro_usd: int = 0
    month_micro_usd: int = 0
    execution_micro_usd: int = 0
    execution_tokens: int = 0


class UsageLedger(Protocol):
    async def record(self, entry: UsageEntry) -> None: ...

    async def limits(self, organization_id: uuid.UUID) -> OrgLimits: ...

    async def spent(self, ctx: CallContext) -> Spend: ...


# --------------------------------------------------------------------------- service


@dataclass
class RetryConfig:
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 20.0


@dataclass
class LLMService:
    providers: dict[str, LLMProvider]
    prices: PriceTable
    ledger: UsageLedger
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    retry: RetryConfig = field(default_factory=RetryConfig)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    # Providers that enforce the schema server-side get the schema natively instead of
    # as prompt text; everyone still gets client-side validation.
    # (The mock emulates a native provider: it generates schema-valid JSON by default.)
    native_structured_output: frozenset[str] = frozenset({"anthropic", "mock"})

    def available_models(self) -> list[str]:
        return [m for m in self.prices.models() if ModelRef.parse(m).provider in self.providers]

    def is_known(self, ref: ModelRef) -> bool:
        return ref.provider in self.providers and self.prices.get(ref) is not None

    async def generate(self, call: LLMCall, ctx: CallContext) -> LLMResult:
        with tracer().start_as_current_span(
            "llm.generate",
            attributes={
                "sf.llm.purpose": ctx.purpose,
                "sf.llm.models": [str(m) for m in call.models],
            },
        ) as span:
            try:
                result = await self._generate(call, ctx)
            except LLMError as exc:
                span.set_status(Status(StatusCode.ERROR, exc.code))
                raise
            span.set_attribute("sf.llm.model", str(result.model))
            span.set_attribute("sf.llm.cost_micro_usd", result.cost_micro_usd)
            return result

    async def _generate(self, call: LLMCall, ctx: CallContext) -> LLMResult:
        if not call.models:
            raise InvalidRequest("no model specified")
        for ref in call.models:
            if self.prices.get(ref) is None:
                raise InvalidRequest(f"model {ref} has no price entry; refusing unmetered call")

        errors: list[LLMError] = []
        total = _Totals()
        for index, ref in enumerate(call.models):
            try:
                return await self._with_model(call, ctx, ref, index, total)
            except (BudgetExceeded, InvalidRequest):
                raise
            except LLMError as exc:
                errors.append(exc)
                log.warning("llm_model_failed", model=str(ref), code=exc.code, fallback_index=index)
        raise AllModelsFailed(
            f"all {len(call.models)} model(s) failed: " + ", ".join(e.code for e in errors),
            errors=errors,
            usage=total.usage,
        )

    async def _with_model(
        self, call: LLMCall, ctx: CallContext, ref: ModelRef, index: int, total: _Totals
    ) -> LLMResult:
        provider = self.providers.get(ref.provider)
        if provider is None:
            raise ProviderAuthError(f"provider '{ref.provider}' is not configured")
        price = self.prices.get(ref)
        assert price is not None
        key = str(ref)
        native = ref.provider in self.native_structured_output
        system = call.system
        if call.output_schema is not None and not native:
            system = "\n\n".join(
                p for p in (system, structured.schema_instruction(call.output_schema)) if p
            )
        messages = list(call.messages)
        transient_failures = 0
        repairs = 0

        while True:
            if not self.breaker.allow(key):
                raise CircuitOpen(f"circuit open for {key}")
            await self._check_budget(ctx, call, price, system, messages)

            total.calls += 1
            request = ProviderRequest(
                model=ref.model,
                messages=tuple(messages),
                system=system,
                max_tokens=call.max_tokens,
                output_schema=call.output_schema if native else None,
                effort=call.effort,
                timeout_seconds=call.timeout_seconds,
            )
            t0 = time.monotonic()
            try:
                async with asyncio.timeout(call.timeout_seconds):
                    response = await provider.complete(request)
            except TimeoutError:
                err: LLMError = ProviderUnavailable(
                    f"{key} timed out after {call.timeout_seconds}s"
                )
                response = None
            except LLMError as exc:
                err = exc
                response = None
            latency_ms = int((time.monotonic() - t0) * 1000)

            if response is None:
                await self._meter(
                    ctx,
                    ref,
                    err.code,
                    err.usage,
                    price.cost_micro_usd(err.usage),
                    latency_ms,
                    total,
                    index,
                    None,
                )
                if err.retryable:
                    self.breaker.record_failure(key)
                    transient_failures += 1
                    if transient_failures < call.attempts_per_model:
                        await self.sleep(self._backoff(transient_failures, err))
                        continue
                raise err

            self.breaker.record_success(key)
            cost = price.cost_micro_usd(response.usage)
            outcome, parsed, failure = self._assess(response.text, response.stop_reason, call)
            await self._meter(
                ctx,
                ref,
                outcome,
                response.usage,
                cost,
                latency_ms,
                total,
                index,
                response.request_id,
            )
            if failure is None:
                return LLMResult(
                    text=response.text,
                    json=parsed,
                    model=ref,
                    usage=total.usage,
                    cost_micro_usd=total.cost,
                    calls=total.calls,
                    fallback_used=index > 0,
                    attempts=total.attempts,
                )
            if isinstance(failure, OutputInvalid) and repairs < call.max_repairs:
                repairs += 1
                messages += [
                    Message("assistant", response.text or "(empty)"),
                    Message("user", structured.repair_message(failure.details["errors"])),
                ]
                continue
            raise failure

    def _assess(
        self, text: str, stop_reason: str, call: LLMCall
    ) -> tuple[str, Any | None, LLMError | None]:
        if stop_reason == "refusal":
            return "refused", None, Refused("model refused the request")
        if stop_reason == "max_tokens":
            return "truncated", None, Truncated(f"output hit max_tokens={call.max_tokens}")
        if call.output_schema is None:
            return "ok", None, None
        value, errors = structured.parse_and_validate(text, call.output_schema)
        if errors:
            return "output_invalid", None, OutputInvalid("structured output invalid", errors=errors)
        return "ok", value, None

    async def _check_budget(
        self,
        ctx: CallContext,
        call: LLMCall,
        price: Any,
        system: str | None,
        messages: list[Message],
    ) -> None:
        est_in = estimate_tokens((system or "") + "".join(m.content for m in messages))
        worst = price.worst_case_micro_usd(est_in, call.max_tokens)
        limits = await self.ledger.limits(ctx.organization_id)
        spend = await self.ledger.spent(ctx)

        per_exec = _min_limit(limits.per_execution_micro_usd, ctx.execution_cost_limit_micro_usd)
        checks = [
            ("daily", spend.day_micro_usd, limits.daily_micro_usd),
            ("monthly", spend.month_micro_usd, limits.monthly_micro_usd),
        ]
        if ctx.execution_id is not None:
            checks.append(("per_execution", spend.execution_micro_usd, per_exec))
        for scope, spent, limit in checks:
            if limit is not None and spent + worst > limit:
                raise BudgetExceeded(
                    f"{scope} LLM budget would be exceeded",
                    scope=scope,
                    spent_micro_usd=spent,
                    limit_micro_usd=limit,
                    call_worst_case_micro_usd=worst,
                )
        token_limit = ctx.execution_token_limit if ctx.execution_id is not None else None
        if (
            token_limit is not None
            and spend.execution_tokens + est_in + call.max_tokens > token_limit
        ):
            raise BudgetExceeded(
                "execution token budget would be exceeded",
                scope="execution_tokens",
                spent_tokens=spend.execution_tokens,
                limit_tokens=token_limit,
            )

    async def _meter(
        self,
        ctx: CallContext,
        ref: ModelRef,
        outcome: str,
        usage: Usage,
        cost: int,
        latency_ms: int,
        total: _Totals,
        index: int,
        request_id: str | None,
    ) -> None:
        total.add(usage, cost)
        total.attempts.append(
            {
                "model": str(ref),
                "outcome": outcome,
                "latency_ms": latency_ms,
                "cost_micro_usd": cost,
            }
        )
        await self.ledger.record(
            UsageEntry(
                organization_id=ctx.organization_id,
                execution_id=ctx.execution_id,
                step_id=ctx.step_id,
                purpose=ctx.purpose,
                provider=ref.provider,
                model=ref.model,
                outcome=outcome,
                usage=usage,
                cost_micro_usd=cost,
                latency_ms=latency_ms,
                attempt=total.calls,
                fallback_index=index,
                request_id=request_id,
            )
        )
        labels = (ref.provider, ref.model)
        metrics.LLM_CALLS.labels(*labels, outcome).inc()
        metrics.LLM_TOKENS.labels(*labels, "input").inc(usage.input_tokens)
        metrics.LLM_TOKENS.labels(*labels, "output").inc(usage.output_tokens)
        metrics.LLM_COST.labels(*labels).inc(cost / 1_000_000)
        metrics.LLM_DURATION.labels(*labels).observe(latency_ms / 1000)
        trace.get_current_span().add_event(
            "llm.attempt",
            {
                "model": str(ref),
                "outcome": outcome,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_micro_usd": cost,
                "latency_ms": latency_ms,
            },
        )
        # Never log prompts or outputs (may contain customer data); metadata only.
        log.info(
            "llm_call",
            model=str(ref),
            outcome=outcome,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_micro_usd=cost,
            latency_ms=latency_ms,
            execution_id=str(ctx.execution_id) if ctx.execution_id else None,
        )

    def _backoff(self, failures: int, err: LLMError) -> float:
        delay: float = min(
            self.retry.backoff_max_seconds,
            self.retry.backoff_base_seconds * 2.0 ** (failures - 1),
        )
        if isinstance(err, RateLimited) and err.retry_after:
            delay = min(self.retry.backoff_max_seconds, max(delay, err.retry_after))
        return delay * random.uniform(0.9, 1.1)  # noqa: S311 - jitter, not crypto


@dataclass
class _Totals:
    usage: Usage = field(default_factory=Usage)
    cost: int = 0
    calls: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def add(self, u: Usage, cost: int) -> None:
        self.usage = Usage(
            self.usage.input_tokens + u.input_tokens,
            self.usage.output_tokens + u.output_tokens,
            self.usage.cache_read_tokens + u.cache_read_tokens,
            self.usage.cache_write_tokens + u.cache_write_tokens,
        )
        self.cost += cost


def _min_limit(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
