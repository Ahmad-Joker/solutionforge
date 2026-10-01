"""The ``agent`` step: a bounded, tool-using LLM loop (see agents/runner.py)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from solutionforge.agents.runner import AgentConfig, AgentStopped, run_agent
from solutionforge.llm import structured
from solutionforge.llm.pricing import micro_to_usd
from solutionforge.llm.service import LLMService
from solutionforge.llm.types import (
    AllModelsFailed,
    BudgetExceeded,
    CallContext,
    InvalidRequest,
    LLMError,
    ModelRef,
)
from solutionforge.tools.executor import ToolExecutor
from solutionforge.workflows import expressions
from solutionforge.workflows.registry import StepContext, StepError, StepHandler, StepResult


class AgentStepConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    fallback_models: list[str] = Field(default_factory=list, max_length=3)
    task: str = Field(min_length=1, max_length=20_000, description="Template for the goal")
    instructions: str | None = Field(default=None, max_length=10_000)
    tools: list[str] = Field(default_factory=list, max_length=20)
    output_schema: dict[str, Any] | None = None
    max_turns: int = Field(default=8, ge=1, le=20)
    max_tool_calls: int = Field(default=6, ge=0, le=50)
    max_tokens_per_turn: int = Field(default=1024, ge=64, le=16_000)

    @field_validator("output_schema")
    @classmethod
    def _schema(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None:
            structured.check_schema(v)
        return v

    @model_validator(mode="after")
    def _against_runtime(self, info: ValidationInfo) -> AgentStepConfig:
        context = info.context or {}
        llm: LLMService | None = context.get("llm_service")
        executor: ToolExecutor | None = context.get("tool_executor")
        for ref in [self.model, *self.fallback_models]:
            parsed = ModelRef.parse(ref)
            if llm is not None and not llm.is_known(parsed):
                raise ValueError(f"unknown or unpriced model {ref!r}")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("duplicate tools in allowlist")
        if executor is not None:
            unknown = [t for t in self.tools if not executor.catalog.has(t)]
            if unknown:
                raise ValueError(f"unknown tools in allowlist: {unknown}")
        return self


class AgentStep(StepHandler[AgentStepConfig]):
    type = "agent"
    config_model = AgentStepConfig
    description = "Bounded tool-using agent: allowlisted tools, policy-gated, capped turns."

    def __init__(self, llm: LLMService, executor: ToolExecutor) -> None:
        self.llm = llm
        self.executor = executor

    def parse_config(self, raw: dict[str, Any]) -> AgentStepConfig:
        return AgentStepConfig.model_validate(
            raw, context={"llm_service": self.llm, "tool_executor": self.executor}
        )

    def references(self, config: AgentStepConfig) -> list[str]:
        return expressions.references([config.task, config.instructions or ""])

    async def run(self, config: AgentStepConfig, ctx: StepContext) -> StepResult:
        try:
            task = str(expressions.resolve(config.task, ctx.scope))
            instructions = (
                str(expressions.resolve(config.instructions, ctx.scope))
                if config.instructions
                else None
            )
        except expressions.ExpressionError as exc:
            raise StepError(str(exc), code="expression_error") from exc

        cfg = AgentConfig(
            models=tuple(ModelRef.parse(m) for m in [config.model, *config.fallback_models]),
            task=task,
            tools=tuple(config.tools),
            instructions=instructions,
            output_schema=config.output_schema,
            max_turns=config.max_turns,
            max_tool_calls=config.max_tool_calls,
            max_tokens_per_turn=config.max_tokens_per_turn,
        )
        call_ctx = CallContext(
            organization_id=ctx.organization_id,
            execution_id=ctx.execution_id,
            step_id=ctx.step_id,
            execution_cost_limit_micro_usd=ctx.cost_limit_micro_usd,
            execution_token_limit=ctx.llm_token_limit,
        )
        try:
            result = await run_agent(
                cfg,
                llm=self.llm,
                executor=self.executor,
                ctx=call_ctx,
                key_prefix=ctx.idempotency_key,
            )
        except AgentStopped as exc:
            raise StepError(exc.message, code=exc.code, details={"trace": exc.trace}) from exc
        except BudgetExceeded as exc:
            raise StepError(
                exc.message, code="llm_budget_exceeded", details=exc.details, exhausts_budget=True
            ) from exc
        except InvalidRequest as exc:
            raise StepError(exc.message, code="llm_invalid_request") from exc
        except AllModelsFailed as exc:
            raise StepError(exc.message, code="llm_failed", retryable=exc.retryable) from exc
        except LLMError as exc:
            raise StepError(exc.message, code=f"llm_{exc.code}", retryable=exc.retryable) from exc

        return StepResult(
            output={
                "answer": result.answer,
                "turns": result.turns,
                "tool_calls": result.tool_calls,
                "cost_usd": str(micro_to_usd(result.cost_micro_usd)),
                "trace": result.trace,
            }
        )
