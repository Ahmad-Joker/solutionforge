"""Tool contract: spec, risk levels, execution context and error taxonomy.

A tool is a class with a :class:`ToolSpec` and an async ``execute``. Inputs and outputs are
Pydantic models (``extra="forbid"``, bounded lengths), so argument validation happens in
one place before any side effect, and JSON Schemas for LLM tool use and MCP come for free.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.security.rbac import Permission

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{1,31}\.[a-z][a-z0-9_]{1,63}$"


class RiskLevel(StrEnum):
    READ_ONLY = "read_only"  # no side effects
    LOW_RISK_WRITE = "low_risk_write"  # internal, reversible writes (create a ticket, a draft)
    EXTERNAL_ACTION = "external_action"  # leaves the org boundary (send email): needs approval
    HIGH_RISK = "high_risk"  # money / irreversible (refund): needs privileged approval


RISK_ORDER = [
    RiskLevel.READ_ONLY,
    RiskLevel.LOW_RISK_WRITE,
    RiskLevel.EXTERNAL_ACTION,
    RiskLevel.HIGH_RISK,
]


class ToolModel(BaseModel):
    """Base for tool inputs/outputs: unknown fields are rejected, never ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[ToolModel]
    output_model: type[ToolModel]
    risk_level: RiskLevel
    required_permission: Permission
    timeout_seconds: float = 10.0
    max_attempts: int = 1  # retries only for transient errors
    # Safe to repeat with the same arguments? Writes are only retried when the tool
    # deduplicates by idempotency key (``supports_idempotency_key``).
    idempotent: bool = False
    supports_idempotency_key: bool = False
    requires_credentials: bool = False
    config_keys: tuple[str, ...] = ()

    @property
    def has_side_effects(self) -> bool:
        return self.risk_level != RiskLevel.READ_ONLY

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()

    def mcp_descriptor(self) -> dict[str, Any]:
        """Tool description in the Model Context Protocol ``tools/list`` shape, so the catalog
        can be served to MCP clients and fed to LLM tool use unchanged."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema(),
            "outputSchema": self.output_schema(),
            "annotations": {
                "readOnlyHint": self.risk_level == RiskLevel.READ_ONLY,
                "destructiveHint": self.risk_level == RiskLevel.HIGH_RISK,
                "idempotentHint": self.idempotent,
                "openWorldHint": self.risk_level
                in (RiskLevel.EXTERNAL_ACTION, RiskLevel.HIGH_RISK),
            },
        }


SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool may use. Tools must scope every query to ``organization_id``."""

    organization_id: uuid.UUID
    execution_id: uuid.UUID | None
    step_id: str | None
    idempotency_key: str | None
    config: dict[str, Any]
    credentials: dict[str, Any] | None
    session: SessionFactory = field(repr=False)


class Tool(ABC):
    spec: ClassVar[ToolSpec]

    @abstractmethod
    async def execute(self, args: Any, ctx: ToolContext) -> ToolModel: ...


# --------------------------------------------------------------------------- errors


class ToolError(Exception):
    code = "tool_error"
    retryable = False

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ToolNotFound(ToolError):
    code = "tool_not_found"


class ToolNotEnabled(ToolError):
    code = "tool_not_enabled"


class ToolInputInvalid(ToolError):
    code = "tool_input_invalid"


class ToolDenied(ToolError):
    code = "tool_denied"


class ToolApprovalRequired(ToolError):
    code = "tool_approval_required"


class ToolConfigError(ToolError):
    """Missing/invalid installation config or credentials."""

    code = "tool_config_error"


class ToolBusinessError(ToolError):
    """The target system rejected the operation (unknown customer, refund > order total)."""

    code = "tool_business_error"


class ToolTransientError(ToolError):
    code = "tool_transient_error"
    retryable = True


class ToolTimeout(ToolError):
    code = "tool_timeout"
    retryable = True


class ToolIdempotencyConflict(ToolError):
    """The idempotency key was already used for a *different* request: refusing to guess
    whether to replay or execute (same rule as payment APIs)."""

    code = "tool_idempotency_conflict"


class ToolOutputInvalid(ToolError):
    """The tool returned data not matching its own output schema (a connector bug)."""

    code = "tool_output_invalid"
