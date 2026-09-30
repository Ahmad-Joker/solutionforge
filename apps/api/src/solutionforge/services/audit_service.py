"""Recording and querying audit events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.db.tenancy import TenantContext
from solutionforge.domain.audit import AuditEvent, AuditEventType
from solutionforge.security.rbac import Permission
from solutionforge.services.authz import ensure

REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "cookie",
    "private_key",
)
_MAX_DEPTH = 6


@dataclass(frozen=True, slots=True)
class RequestMeta:
    ip_address: str | None = None
    request_id: str | None = None


def sanitize_metadata(value: Any, _depth: int = 0) -> Any:
    """Redact values under secret-looking keys, recursively. Audit rows never hold secrets."""
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(k): REDACTED
            if any(p in str(k).lower() for p in _SENSITIVE_KEY_PARTS)
            else sanitize_metadata(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize_metadata(v, _depth + 1) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def record(
    session: AsyncSession,
    *,
    event_type: AuditEventType,
    request: RequestMeta,
    actor_user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | str | None = None,
    execution_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Stage an audit event in the caller's transaction, so it commits atomically with the
    action it describes (or not at all)."""
    event = AuditEvent(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type=event_type.value,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        execution_id=execution_id,
        event_metadata=sanitize_metadata(metadata or {}),
        ip_address=request.ip_address,
        request_id=request.request_id,
    )
    session.add(event)
    return event


async def list_events(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    limit: int = 50,
    before: datetime | None = None,
    event_type: str | None = None,
) -> list[AuditEvent]:
    ensure(ctx, Permission.AUDIT_READ)
    stmt = sa.select(AuditEvent).where(AuditEvent.organization_id == ctx.organization_id)
    if before is not None:
        stmt = stmt.where(AuditEvent.created_at < before)
    if event_type is not None:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    stmt = stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())
