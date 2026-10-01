"""Append-only audit log.

The application only ever INSERTs audit rows. On PostgreSQL a trigger (see the initial
migration) additionally rejects UPDATE and DELETE, so immutability does not depend on
application code being bug-free.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from solutionforge.core.clock import utcnow
from solutionforge.db.base import Base, JSONType, UUIDPrimaryKeyMixin


class AuditEventType(StrEnum):
    USER_REGISTERED = "user.registered"
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    REFRESH_TOKEN_REUSE = "auth.refresh_token_reuse_detected"  # noqa: S105

    ORG_CREATED = "org.created"
    ORG_UPDATED = "org.updated"

    MEMBER_ADDED = "member.added"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    INVITATION_CREATED = "invitation.created"
    INVITATION_ACCEPTED = "invitation.accepted"
    INVITATION_REVOKED = "invitation.revoked"

    WORKFLOW_CREATED = "workflow.created"
    WORKFLOW_VERSION_CREATED = "workflow.version_created"
    WORKFLOW_DEPLOYED = "workflow.deployed"
    EXECUTION_CREATED = "execution.created"
    EXECUTION_CANCELLED = "execution.cancelled"
    EXECUTION_RESUMED = "execution.resumed"

    BUDGET_UPDATED = "budget.updated"

    TOOL_INSTALLATION_UPDATED = "tool.installation_updated"
    CREDENTIALS_UPDATED = "tool.credentials_updated"  # metadata never includes values
    TOOL_EXECUTED = "tool.executed"  # side-effecting tools only
    TOOL_DENIED = "tool.denied"
    DEMO_DATA_SEEDED = "org.demo_data_seeded"


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (sa.Index("ix_audit_events_org_created", "organization_id", "created_at"),)

    # Nullable: some events (login, registration) are user-scoped, not tenant-scoped.
    # No FK on purpose: audit history must outlive the rows it describes.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    ip_address: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
