"""The ``llm`` step: a templated prompt → text or schema-validated JSON, via LLMService."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from solutionforge.llm import structured
from solutionforge.llm.pricing import micro_to_usd
from solutionforge.llm.service import LLMService
from solutionforge.llm.types import (
    AllModelsFailed,
    BudgetExceeded,
    CallContext,
    InvalidRequest,
    LLMCall,
    LLMError,
    Message,
    ModelRef,
)
from solutionforge.workflows import expressions
from solutionforge.workflows.registry import StepContext, StepError, StepHandler, StepResult


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="provider:model; pinned in the version for provenance")
    fallback_models: list[str] = Field(default_factory=list, max_length=3)
    system: str | None = Field(default=None, max_length=20_000)
    prompt: str = Field(min_length=1, max_length=50_000, description="Template for the user turn")
    output_schema: dict[str, Any] | None = None
    max_tokens: int = Field(default=1024, ge=1, le=64_000)
    max_repairs: int = Field(default=1, ge=0, le=3)
    attempts_per_model: int = Field(default=2, ge=1, le=5)
    call_timeout_seconds: float = Field(default=60, gt=0, le=600)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    @field_validator("model", "fallback_models")
    @classmethod
    def _known_models(cls, v: Any, info: ValidationInfo) -> Any:
        service: LLMService | None = (info.context or {}).get("llm_service")
        for ref in [v] if isinstance(v, str) else v:
            parsed = ModelRef.parse(ref)  # ValueError -> validation error
            if service is not None and not service.is_known(parsed):
                raise ValueError(
                    f"unknown or unpriced model {ref!r}; available: {service.available_models()}"
                )
        return v

    @field_validator("output_schema")
    @classmethod
    def _valid_schema(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None:
            structured.check_schema(v)
        return v


class LLMStep(StepHandler[LLMConfig]):
    type = "llm"
    config_model = LLMConfig
    description = "Call a language model; optionally enforce a JSON Schema on the output."

    def __init__(self, service: LLMService) -> None:
        self.service = service

    def parse_config(self, raw: dict[str, Any]) -> LLMConfig:
        return LLMConfig.model_validate(raw, context={"llm_service": self.service})

    def references(self, config: LLMConfig) -> list[str]:
        # The output schema is data, not expressions: never scan it for references.
        return expressions.references([config.system or "", config.prompt])

    async def run(self, config: LLMConfig, ctx: StepContext) -> StepResult:
        try:
            system = expressions.resolve(config.system, ctx.scope) if config.system else None
            prompt = expressions.resolve(config.prompt, ctx.scope)
        except expressions.ExpressionError as exc:
            raise StepError(str(exc), code="expression_error") from exc

        call = LLMCall(
            models=tuple(ModelRef.parse(m) for m in [config.model, *config.fallback_models]),
            messages=(Message("user", str(prompt)),),
            system=str(system) if system is not None else None,
            max_tokens=config.max_tokens,
            output_schema=config.output_schema,
            effort=config.effort,
            max_repairs=config.max_repairs,
            attempts_per_model=config.attempts_per_model,
            timeout_seconds=config.call_timeout_seconds,
        )
        call_ctx = CallContext(
            organization_id=ctx.organization_id,
            execution_id=ctx.execution_id,
            step_id=ctx.step_id,
            execution_cost_limit_micro_usd=ctx.cost_limit_micro_usd,
            execution_token_limit=ctx.llm_token_limit,
        )
        try:
            result = await self.service.generate(call, call_ctx)
        except BudgetExceeded as exc:
            raise StepError(
                exc.message, code="llm_budget_exceeded", details=exc.details, exhausts_budget=True
            ) from exc
        except InvalidRequest as exc:
            raise StepError(exc.message, code="llm_invalid_request") from exc
        except AllModelsFailed as exc:
            raise StepError(
                exc.message,
                code="llm_failed",
                retryable=exc.retryable,  # all-transient → the engine may retry the step later
                details={"errors": [_err(e) for e in exc.errors]},
            ) from exc
        except LLMError as exc:
            raise StepError(exc.message, code=f"llm_{exc.code}", retryable=exc.retryable) from exc

        return StepResult(
            output={
                "text": result.text,
                "json": result.json,
                "model": str(result.model),
                "fallback_used": result.fallback_used,
                "calls": result.calls,
                "usage": {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "cost_usd": str(micro_to_usd(result.cost_micro_usd)),
                },
            }
        )


def _err(e: LLMError) -> dict[str, Any]:
    out: dict[str, Any] = {"code": e.code, "message": e.message}
    if "errors" in e.details:
        out["validation_errors"] = e.details["errors"]
    return out
