"""Provider-neutral request/response types and the error taxonomy."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ModelRef:
    """``provider:model``, e.g. ``anthropic:claude-opus-5`` or ``mock:mock-1``."""

    provider: str
    model: str

    @classmethod
    def parse(cls, ref: str) -> ModelRef:
        provider, sep, model = ref.partition(":")
        if not sep or not provider or not model:
            raise ValueError(f"model must be 'provider:model', got {ref!r}")
        return cls(provider, model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """What a provider adapter receives: one attempt against one model."""

    model: str
    messages: tuple[Message, ...]
    system: str | None = None
    max_tokens: int = 1024
    output_schema: dict[str, Any] | None = None
    effort: str | None = None
    timeout_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    usage: Usage
    stop_reason: str
    request_id: str | None = None


class LLMProvider(Protocol):
    name: str

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """One attempt. Raise :class:`LLMError` subclasses; never retry internally."""
        ...


# --------------------------------------------------------------------------- errors


class LLMError(Exception):
    code: str = "llm_error"
    retryable: bool = False

    def __init__(self, message: str, *, usage: Usage | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage or Usage()
        self.details = details


class RateLimited(LLMError):
    code = "rate_limited"
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ProviderUnavailable(LLMError):
    """5xx / overloaded / connection failure / timeout."""

    code = "provider_unavailable"
    retryable = True


class InvalidRequest(LLMError):
    """The request itself is wrong (400/404/413/422): another model won't fix a bad request."""

    code = "invalid_request"


class ProviderAuthError(LLMError):
    """Credentials missing/invalid for this provider; other providers may still work."""

    code = "provider_auth_error"


class Refused(LLMError):
    code = "refused"


class Truncated(LLMError):
    """Hit max_tokens before finishing."""

    code = "truncated"


class OutputInvalid(LLMError):
    """Structured output failed JSON parsing or schema validation after all repairs."""

    code = "output_invalid"


class CircuitOpen(LLMError):
    code = "circuit_open"


class BudgetExceeded(LLMError):
    code = "budget_exceeded"


class AllModelsFailed(LLMError):
    code = "all_models_failed"

    def __init__(self, message: str, *, errors: list[LLMError], **kw: Any) -> None:
        super().__init__(message, **kw)
        self.errors = errors
        # Transient if every model failed transiently: the step may retry later.
        self.retryable = all(e.retryable or isinstance(e, CircuitOpen) for e in errors)


# --------------------------------------------------------------------------- service-level


@dataclass(frozen=True, slots=True)
class CallContext:
    """Who is paying and why. Every metered call is attributed to an organization."""

    organization_id: uuid.UUID
    execution_id: uuid.UUID | None = None
    step_id: str | None = None
    purpose: str = "workflow_step"
    execution_cost_limit_micro_usd: int | None = None
    execution_token_limit: int | None = None


@dataclass(frozen=True, slots=True)
class LLMCall:
    models: tuple[ModelRef, ...]  # primary first, then fallbacks
    messages: tuple[Message, ...]
    system: str | None = None
    max_tokens: int = 1024
    output_schema: dict[str, Any] | None = None
    effort: str | None = None
    max_repairs: int = 1
    attempts_per_model: int = 2
    timeout_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    json: Any | None
    model: ModelRef
    usage: Usage  # summed over every attempt of this call (incl. failed/repaired ones)
    cost_micro_usd: int
    calls: int
    fallback_used: bool
    attempts: list[dict[str, Any]] = field(default_factory=list)
