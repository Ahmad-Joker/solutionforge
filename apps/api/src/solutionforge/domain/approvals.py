"""Human approval requests for policy-gated tool calls."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from solutionforge.core.clock import utcnow
from solutionforge.db.base import Base, JSONType, TenantScopedMixin, UUIDPrimaryKeyMixin


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Approval(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """One request per (execution, step visit). The engine re-runs the step after approval
    and the executor consumes the approval exactly once."""

    __tablename__ = "approvals"
    __table_args__ = (
        sa.UniqueConstraint("execution_id", "step_id", "visit"),
        sa.Index("ix_approvals_org_status", "organization_id", "status"),
        sa.Index("ix_approvals_expiry", "status", "expires_at"),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    visit: Mapped[int] = mapped_column(nullable=False)
    tool_name: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    risk_level: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    required_permission: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    proposed_args: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    approved_args: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        sa.Enum(
            ApprovalStatus,
            name="approval_status",
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
        ),
        nullable=False,
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    comment: Mapped[str | None] = mapped_column(sa.String(2000), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
