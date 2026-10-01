"""Step handler contract and registry.

A step type is a class with a Pydantic ``config_model`` (validated when a version is created,
not at run time) and an async ``run``. Handlers are stateless; everything they need arrives
in :class:`StepContext`. They never touch the database directly: the engine owns persistence,
so a handler cannot corrupt execution state or bypass checkpointing.
"""

from __future__ import annotations

import builtins
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

from pydantic import BaseModel

from solutionforge.workflows import expressions


class StepError(Exception):
    """A step failed in a way the handler understands.

    ``retryable=True`` means a later attempt may succeed (timeouts, 5xx, rate limits).
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str = "step_failed",
        details: dict[str, Any] | None = None,
        exhausts_budget: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.code = code
        self.details = details or {}
        # Ends the execution as budget_exceeded (not failed), bypassing retries/on_error:
        # a fallback path must not be a way to keep spending past a budget.
        self.exhausts_budget = exhausts_budget


class _UseDefault:
    def __repr__(self) -> str:
        return "USE_DEFAULT"


USE_DEFAULT: Final = _UseDefault()


@dataclass(frozen=True, slots=True)
class Suspend:
    """Pause the execution until an external signal (``resume``) arrives."""

    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StepResult:
    output: dict[str, Any] = field(default_factory=dict)
    set_state: dict[str, Any] = field(default_factory=dict)
    # Branching steps override the static ``next``. ``None`` ends the workflow.
    goto: str | _UseDefault | None = USE_DEFAULT
    suspend: Suspend | None = None


@dataclass(frozen=True, slots=True)
class StepContext:
    organization_id: uuid.UUID
    execution_id: uuid.UUID
    workflow_id: uuid.UUID
    step_id: str
    attempt: int
    scope: expressions.Scope  # read-only snapshot: input / state / steps
    deadline: float  # time.monotonic() value; the engine also enforces it
    cost_limit_micro_usd: int | None = None  # from the definition's limits.max_cost_usd
    llm_token_limit: int | None = None  # from limits.max_llm_tokens
    visit: int = 0  # execution.visit_seq at the time this step visit started
    # Current role of the execution's creator, resolved when the step starts (None = no
    # longer a member). Tool policy uses it; it is never taken from execution input.
    initiator_role: str | None = None

    @property
    def idempotency_key(self) -> str:
        """Stable across retries and crash-recovery of the *same* step visit (so a side
        effect such as creating a ticket happens once), but different for each visit of a
        step inside a loop (so the loop's second iteration really runs)."""
        return f"{self.execution_id}:{self.step_id}:{self.visit}"

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


class StepHandler[C: BaseModel](ABC):
    type: ClassVar[str]
    # builtins.type: inside this class body, bare ``type`` would mean the attribute above.
    config_model: ClassVar[builtins.type[BaseModel]]
    description: ClassVar[str] = ""

    def parse_config(self, raw: dict[str, Any]) -> C:
        return self.config_model.model_validate(raw)  # type: ignore[return-value]

    def targets(self, config: C) -> list[str | None]:
        """Step ids this step may jump to besides ``next``/``on_error`` (for graph validation)."""
        return []

    def references(self, config: C) -> list[str]:
        """Expression references used by the config (validated statically)."""
        return expressions.references(config.model_dump(mode="json", by_alias=True))

    @abstractmethod
    async def run(self, config: C, ctx: StepContext) -> StepResult: ...

    async def resume(self, config: C, ctx: StepContext, payload: dict[str, Any]) -> StepResult:
        """Called when a suspended step receives its signal. Default: payload becomes output."""
        return StepResult(output=payload)


class UnknownStepType(KeyError):
    pass


class StepRegistry:
    def __init__(self, handlers: list[StepHandler[Any]] | None = None) -> None:
        self._handlers: dict[str, StepHandler[Any]] = {}
        for h in handlers or []:
            self.register(h)

    def register(self, handler: StepHandler[Any]) -> None:
        if handler.type in self._handlers:
            raise ValueError(f"step type already registered: {handler.type}")
        self._handlers[handler.type] = handler

    def get(self, step_type: str) -> StepHandler[Any]:
        try:
            return self._handlers[step_type]
        except KeyError:
            raise UnknownStepType(step_type) from None

    def types(self) -> list[str]:
        return sorted(self._handlers)

    def copy(self) -> StepRegistry:
        return StepRegistry(list(self._handlers.values()))
