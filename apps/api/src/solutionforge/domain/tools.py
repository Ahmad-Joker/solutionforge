"""Per-tenant tool installations and the tool-call trail."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from solutionforge.core.clock import utcnow
from solutionforge.db.base import (
    Base,
    JSONType,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class ToolCallStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"


class ToolInstallation(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """An organization's opt-in to a tool. Tools are unusable until installed and enabled."""

    __tablename__ = "tool_installations"
    __table_args__ = (sa.UniqueConstraint("organization_id", "tool_name"),)

    tool_name: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    # Tenant policy knob for LOW_RISK_WRITE tools (see tools/policy.py).
    auto_approve_low_risk: Mapped[bool] = mapped_column(nullable=False, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    # Fernet token; never returned by the API, never logged.
    credentials_encrypted: Mapped[bytes | None] = mapped_column(sa.LargeBinary, nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ToolCall(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """One logical tool invocation (retries inside it are counted in ``attempts``).

    ``(organization_id, tool_name, idempotency_key)`` is unique: a repeated invocation with
    the same key returns the recorded result instead of re-executing the side effect.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        sa.UniqueConstraint("organization_id", "tool_name", "idempotency_key"),
        sa.Index("ix_tool_calls_org_created", "organization_id", "created_at"),
        sa.Index("ix_tool_calls_execution", "execution_id"),
    )

    tool_name: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    risk_level: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    step_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
