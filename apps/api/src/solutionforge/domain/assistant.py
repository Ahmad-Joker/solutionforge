"""Business assistant: runs (one user question each), their event log, and ticket drafts.

A ticket draft is the only thing the model can write. It never becomes a ticket by itself:
a human with ``approval:decide`` approves the exact draft content (bound by ``content_hash``),
and only then does application code create the ticket, exactly once.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from solutionforge.core.clock import utcnow
from solutionforge.db.base import Base, JSONType, TenantScopedMixin, UUIDPrimaryKeyMixin


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"  # final answer produced
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # completed, but abstained
    LIMIT_REACHED = "limit_reached"  # turn / tool-call / deadline / budget limit
    CANCELLED = "cancelled"
    FAILED = "failed"  # provider or internal error after bounded retries


class DraftStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AssistantRun(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "assistant_runs"
    __table_args__ = (sa.Index("ix_assistant_runs_org_created", "organization_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    question: Mapped[str] = mapped_column(sa.Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Source IDs cited in the answer that were verified against this run's retrievals.
    citations: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    model: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    # Limits, tool mode, retrieval settings: everything needed to reproduce the run.
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    prompt_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    corpus_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    turns: Mapped[int] = mapped_column(nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(nullable=False, default=0)
    retries: Mapped[int] = mapped_column(nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cache_write_1h_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    # Estimated from list prices. NULL = unknown (no price for the model), never 0.
    cost_micro_usd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    retrieval_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    tool_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AssistantEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """What the user may see about a run: provider calls, tool calls (name, safe argument
    summary, outcome), approvals. Never raw prompts, secrets or private reasoning."""

    __tablename__ = "assistant_events"
    __table_args__ = (sa.UniqueConstraint("run_id", "seq"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("assistant_runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(24), nullable=False)  # provider|tool|approval
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class TicketDraft(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "ticket_drafts"
    __table_args__ = (
        # Duplicate-write protection: one draft per (run, tool_use id).
        sa.UniqueConstraint("organization_id", "idempotency_key"),
        sa.Index("ix_ticket_drafts_org_status", "organization_id", "status"),
    )

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("assistant_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    subject: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # sha256 of the canonical (subject, description); approval must present the same hash.
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ticket_ref: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
