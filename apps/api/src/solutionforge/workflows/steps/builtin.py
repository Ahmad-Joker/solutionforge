"""Built-in, deterministic step types. LLM/tool/retrieval steps arrive in later phases."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from solutionforge.workflows import expressions
from solutionforge.workflows.expressions import Predicate
from solutionforge.workflows.registry import (
    StepContext,
    StepError,
    StepHandler,
    StepResult,
    Suspend,
)

_STATE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- transform


class TransformConfig(_Strict):
    set: dict[str, Any] = Field(default_factory=dict, description="state vars to assign")
    output: dict[str, Any] = Field(default_factory=dict, description="this step's output")

    @field_validator("set")
    @classmethod
    def _valid_names(cls, v: dict[str, Any]) -> dict[str, Any]:
        bad = [k for k in v if not _STATE_KEY.match(k)]
        if bad:
            raise ValueError(f"invalid state variable names: {bad}")
        return v


class TransformStep(StepHandler[TransformConfig]):
    type = "transform"
    config_model = TransformConfig
    description = "Assign workflow state variables and/or produce an output from expressions."

    async def run(self, config: TransformConfig, ctx: StepContext) -> StepResult:
        try:
            return StepResult(
                set_state=expressions.resolve(config.set, ctx.scope),
                output=expressions.resolve(config.output, ctx.scope),
            )
        except expressions.ExpressionError as exc:
            raise StepError(str(exc), code="expression_error") from exc


# --------------------------------------------------------------------------- condition


class Branch(_Strict):
    when: Predicate
    goto: str | None


class ConditionConfig(_Strict):
    branches: list[Branch] = Field(min_length=1, max_length=50)
    default: str | None


class ConditionStep(StepHandler[ConditionConfig]):
    type = "condition"
    config_model = ConditionConfig
    description = "Branch to the first matching step; `null` ends the workflow."

    def targets(self, config: ConditionConfig) -> list[str | None]:
        return [b.goto for b in config.branches] + [config.default]

    def references(self, config: ConditionConfig) -> list[str]:
        return [r for b in config.branches for r in expressions.predicate_references(b.when)]

    async def run(self, config: ConditionConfig, ctx: StepContext) -> StepResult:
        try:
            for i, branch in enumerate(config.branches):
                if expressions.evaluate(branch.when, ctx.scope):
                    return StepResult(output={"branch": i, "goto": branch.goto}, goto=branch.goto)
        except expressions.ExpressionError as exc:
            raise StepError(str(exc), code="expression_error") from exc
        return StepResult(output={"branch": "default", "goto": config.default}, goto=config.default)


# --------------------------------------------------------------------------- approval


class ApprovalConfig(_Strict):
    message: str = Field(min_length=1, max_length=2000)
    on_reject: str | None = Field(default=None, description="Step to run if rejected")


class ApprovalDecision(_Strict):
    approved: bool
    comment: str | None = Field(default=None, max_length=2000)
    data: dict[str, Any] = Field(default_factory=dict)


class ApprovalStep(StepHandler[ApprovalConfig]):
    """Human checkpoint. Phase 8 adds the Approval entity and policy-driven approvals;
    this step provides the durable suspend/resume mechanism they build on."""

    type = "approval"
    config_model = ApprovalConfig
    description = "Suspend until a human approves or rejects."

    def targets(self, config: ApprovalConfig) -> list[str | None]:
        return [config.on_reject]

    async def run(self, config: ApprovalConfig, ctx: StepContext) -> StepResult:
        try:
            message = expressions.resolve(config.message, ctx.scope)
        except expressions.ExpressionError as exc:
            raise StepError(str(exc), code="expression_error") from exc
        return StepResult(suspend=Suspend(reason="approval", details={"message": message}))

    async def resume(
        self, config: ApprovalConfig, ctx: StepContext, payload: dict[str, Any]
    ) -> StepResult:
        decision = ApprovalDecision.model_validate(payload)
        output = decision.model_dump()
        if decision.approved:
            return StepResult(output=output)
        if config.on_reject is not None:
            return StepResult(output=output, goto=config.on_reject)
        raise StepError("Rejected by approver", code="rejected", details=output)


# --------------------------------------------------------------------------- fail


class FailConfig(_Strict):
    message: str = Field(min_length=1, max_length=2000)
    code: str = Field(default="workflow_failed", pattern=r"^[a-z][a-z0-9_]{0,63}$")


class FailStep(StepHandler[FailConfig]):
    type = "fail"
    config_model = FailConfig
    description = "Terminate the execution as failed with a message."

    async def run(self, config: FailConfig, ctx: StepContext) -> StepResult:
        try:
            message = expressions.resolve(config.message, ctx.scope)
        except expressions.ExpressionError as exc:
            raise StepError(str(exc), code="expression_error") from exc
        raise StepError(str(message), code=config.code)


def builtin_handlers() -> list[StepHandler[Any]]:
    return [TransformStep(), ConditionStep(), ApprovalStep(), FailStep()]
