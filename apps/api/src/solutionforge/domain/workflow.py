"""Workflows, immutable versions, deployments, executions and execution steps."""

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


def _str_enum(enum: type[StrEnum], name: str) -> sa.Enum:
    return sa.Enum(
        enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        native_enum=False,
        length=24,
        validate_strings=True,
    )


class ExecutionStatus(StrEnum):
    QUEUED = "queued"  # runnable once run_after <= now
    RUNNING = "running"  # leased by a worker
    WAITING = "waiting"  # suspended on an external signal (e.g. human approval)
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


TERMINAL_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.BUDGET_EXCEEDED,
    }
)


class StepStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # this attempt failed; the step may be retried
    WAITING = "waiting"
    RESUMED = "resumed"


class Workflow(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (sa.UniqueConstraint("organization_id", "name"),)

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str] = mapped_column(sa.String(2000), nullable=False, default="")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class WorkflowVersion(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Immutable once created. Changing a workflow means creating a new version."""

    __tablename__ = "workflow_versions"
    __table_args__ = (sa.UniqueConstraint("workflow_id", "version"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    definition_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    changelog: Mapped[str] = mapped_column(sa.String(2000), nullable=False, default="")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class WorkflowDeployment(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Append-only deployment history. The newest row is production; rollback = new row.

    Quality-gate results (Phase 11) attach to this record.
    """

    __tablename__ = "workflow_deployments"
    __table_args__ = (sa.Index("ix_workflow_deployments_wf_created", "workflow_id", "created_at"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False
    )
    deployed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(sa.String(2000), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Execution(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "executions"
    __table_args__ = (
        sa.Index("ix_executions_claim", "status", "run_after"),
        sa.Index("ix_executions_org_created", "organization_id", "created_at"),
        sa.UniqueConstraint("organization_id", "idempotency_key"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        _str_enum(ExecutionStatus, "execution_status"), nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    input: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    step_outputs: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    # Set while status == WAITING: what the execution is waiting for (e.g. approval message).
    waiting_on: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    current_step: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    current_attempt: Mapped[int] = mapped_column(nullable=False, default=0)
    # Incremented every time the execution enters a step. Part of the idempotency key, so
    # retries of one visit share a key but a loop's second visit gets a fresh one.
    visit_seq: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    steps_used: Mapped[int] = mapped_column(nullable=False, default=0)
    active_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    event_seq: Mapped[int] = mapped_column(nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)

    run_after: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ExecutionStep(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """One row per step attempt (and per suspend/resume event). Append-only."""

    __tablename__ = "execution_steps"
    __table_args__ = (sa.UniqueConstraint("execution_id", "seq"),)

    execution_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("executions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(nullable=False)
    step_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    step_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[StepStatus] = mapped_column(_str_enum(StepStatus, "step_status"), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime] = mapped_column(nullable=False)
    duration_ms: Mapped[int] = mapped_column(nullable=False)
