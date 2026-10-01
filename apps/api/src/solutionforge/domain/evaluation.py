"""Evaluation datasets, cases, runs, per-case results, and deployment decisions."""

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


def _enum(e: type[StrEnum], name: str) -> sa.Enum:
    return sa.Enum(
        e,
        name=name,
        native_enum=False,
        length=16,
        values_callable=lambda x: [m.value for m in x],
        validate_strings=True,
    )


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"


class EvaluationDataset(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "evaluation_datasets"
    __table_args__ = (sa.UniqueConstraint("workflow_id", "name"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str] = mapped_column(sa.String(2000), nullable=False, default="")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class EvaluationCase(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (sa.UniqueConstraint("dataset_id", "name"),)

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    # Validated by evaluation.scorers.CaseExpectations.
    expectations: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class EvaluationRun(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        sa.Index("ix_eval_runs_dataset_version", "dataset_id", "workflow_version_id"),
    )

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus, "eval_run_status"), nullable=False)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(nullable=False)
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class EvaluationResult(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (sa.UniqueConstraint("run_id", "case_id"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("evaluation_cases.id", ondelete="CASCADE"), nullable=False
    )
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("executions.id", ondelete="SET NULL"), nullable=True
    )
    passed: Mapped[bool] = mapped_column(nullable=False)
    scores: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    failures: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)


class DeploymentDecision(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Every gated deploy attempt, allowed or blocked, with the checks that decided it."""

    __tablename__ = "deployment_decisions"
    __table_args__ = (sa.Index("ix_deploy_decisions_wf_created", "workflow_id", "created_at"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False)
    candidate_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    passed: Mapped[bool] = mapped_column(nullable=False)
    overridden: Mapped[bool] = mapped_column(nullable=False, default=False)
    override_reason: Mapped[str | None] = mapped_column(sa.String(2000), nullable=True)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
