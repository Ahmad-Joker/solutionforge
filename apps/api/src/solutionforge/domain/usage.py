"""LLM usage ledger and per-organization budgets."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from solutionforge.core.clock import utcnow
from solutionforge.db.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin


class UsageRecord(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """One row per provider attempt, successful or not. Money in integer micro-USD."""

    __tablename__ = "usage_records"
    __table_args__ = (
        sa.Index("ix_usage_records_org_created", "organization_id", "created_at"),
        sa.Index("ix_usage_records_execution", "execution_id"),
    )

    # No FK: usage history (billing evidence) must outlive the execution row.
    execution_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    step_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    purpose: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    model: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    input_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cost_micro_usd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)
    fallback_index: Mapped[int] = mapped_column(nullable=False, default=0)
    provider_request_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class OrgBudget(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """NULL limit = unlimited. One row per organization."""

    __tablename__ = "org_budgets"
    __table_args__ = (sa.UniqueConstraint("organization_id"),)

    daily_limit_micro_usd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    monthly_limit_micro_usd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    per_execution_limit_micro_usd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
