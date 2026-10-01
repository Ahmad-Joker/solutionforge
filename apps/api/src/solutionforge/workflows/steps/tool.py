"""The ``tool`` step: call a catalog tool with templated arguments via the ToolExecutor."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from solutionforge.security.rbac import Role
from solutionforge.tools.catalog import ToolCatalog
from solutionforge.tools.executor import ToolExecutor
from solutionforge.tools.spec import ToolError
from solutionforge.workflows import expressions
from solutionforge.workflows.registry import StepContext, StepError, StepHandler, StepResult


class ToolStepConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=3, max_length=96)
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_against_catalog(self, info: ValidationInfo) -> ToolStepConfig:
        catalog: ToolCatalog | None = (info.context or {}).get("tool_catalog")
        if catalog is None:
            return self
        if not catalog.has(self.tool):
            raise ValueError(
                f"unknown tool {self.tool!r}; available: {[s.name for s in catalog.specs()]}"
            )
        fields = catalog.get(self.tool).spec.input_model.model_fields
        unknown = sorted(set(self.args) - set(fields))
        if unknown:
            raise ValueError(f"unknown argument(s) for {self.tool}: {unknown}")
        missing = sorted(n for n, f in fields.items() if f.is_required() and n not in self.args)
        if missing:
            raise ValueError(f"missing required argument(s) for {self.tool}: {missing}")
        return self


class ToolStep(StepHandler[ToolStepConfig]):
    type = "tool"
    config_model = ToolStepConfig
    description = "Invoke a tool. Arguments are validated, policy-checked and deduplicated."

    def __init__(self, executor: ToolExecutor) -> None:
        self.executor = executor

    def parse_config(self, raw: dict[str, Any]) -> ToolStepConfig:
        return ToolStepConfig.model_validate(raw, context={"tool_catalog": self.executor.catalog})

    def references(self, config: ToolStepConfig) -> list[str]:
        return expressions.references(config.args)

    async def run(self, config: ToolStepConfig, ctx: StepContext) -> StepResult:
        try:
            args = expressions.resolve(config.args, ctx.scope)
        except expressions.ExpressionError as exc:
            raise StepError(str(exc), code="expression_error") from exc
        try:
            inv = await self.executor.invoke(
                organization_id=ctx.organization_id,
                tool_name=config.tool,
                args=args,
                idempotency_key=ctx.idempotency_key,
                execution_id=ctx.execution_id,
                step_id=ctx.step_id,
                actor_role=Role(ctx.initiator_role) if ctx.initiator_role else None,
            )
        except ToolError as exc:
            raise StepError(
                exc.message, code=exc.code, retryable=exc.retryable, details=exc.details
            ) from exc
        return StepResult(
            output={
                "result": inv.output,
                "replayed": inv.replayed,
                "tool_call_id": str(inv.tool_call_id),
            }
        )
