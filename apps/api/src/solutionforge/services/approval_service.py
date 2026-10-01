"""Approval inbox, decisions, and expiry.

Decision rules (deterministic, enforced here — not in the UI, not by a model):

* the decider needs the permission recorded on the request (``approval:decide`` or
  ``approval:decide_high_risk``), checked against their *current* role;
* four-eyes: nobody approves a high-risk action that their own execution requested;
* modified arguments are re-validated against the tool's input schema;
* the request must still be pending and unexpired; decisions are final.

After recording the decision (and audit event) in the same transaction, the waiting
execution is resumed; the engine then re-runs the tool step, where the executor consumes
the approval and still re-evaluates policy (a block added since the request wins).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.clock import utcnow
from solutionforge.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from solutionforge.core.logging import get_logger
from solutionforge.db.tenancy import TenantContext, scoped_select
from solutionforge.domain.approvals import Approval, ApprovalStatus
from solutionforge.domain.audit import AuditEventType
from solutionforge.domain.workflow import Execution, ExecutionStatus
from solutionforge.security.rbac import Permission
from solutionforge.services import audit_service
from solutionforge.services.audit_service import RequestMeta, sanitize_metadata
from solutionforge.services.authz import ensure
from solutionforge.services.workflow_service import resume_locked
from solutionforge.tools.catalog import ToolCatalog
from solutionforge.tools.spec import RiskLevel
from solutionforge.workflows.registry import StepRegistry

log = get_logger(__name__)
_SYSTEM = RequestMeta()

Choice = Literal["approve", "reject"]


async def list_approvals(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    status: ApprovalStatus | None,
    execution_id: uuid.UUID | None,
    limit: int,
    before: datetime | None,
) -> list[Approval]:
    ensure(ctx, Permission.APPROVAL_READ)
    stmt = scoped_select(Approval, ctx)
    if status is not None:
        stmt = stmt.where(Approval.status == status)
    if execution_id is not None:
        stmt = stmt.where(Approval.execution_id == execution_id)
    if before is not None:
        stmt = stmt.where(Approval.created_at < before)
    stmt = stmt.order_by(Approval.created_at.desc(), Approval.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def get_approval(
    session: AsyncSession, ctx: TenantContext, approval_id: uuid.UUID
) -> Approval:
    ensure(ctx, Permission.APPROVAL_READ)
    row = await session.scalar(scoped_select(Approval, ctx, Approval.id == approval_id))
    if row is None:
        raise NotFound("Approval not found")
    return row


async def decide(
    session: AsyncSession,
    ctx: TenantContext,
    registry: StepRegistry,
    catalog: ToolCatalog,
    *,
    approval_id: uuid.UUID,
    choice: Choice,
    modified_args: dict[str, Any] | None,
    comment: str | None,
    request: RequestMeta,
) -> Approval:
    ensure(ctx, Permission.APPROVAL_DECIDE)
    row = await session.scalar(
        scoped_select(Approval, ctx, Approval.id == approval_id).with_for_update()
    )
    if row is None:
        raise NotFound("Approval not found")
    if row.status != ApprovalStatus.PENDING:
        raise Conflict(f"Approval is already {row.status.value}")
    now = utcnow()
    if row.expires_at <= now:
        raise Conflict("Approval has expired")
    if not ctx.can(Permission(row.required_permission)):
        raise PermissionDenied(
            f"Deciding this request requires '{row.required_permission}'",
            details={"required_permission": row.required_permission},
        )
    if row.risk_level == RiskLevel.HIGH_RISK.value and row.requested_by_user_id == ctx.user_id:
        raise PermissionDenied(
            "Four-eyes rule: you cannot approve a high-risk action you requested"
        )

    if modified_args is not None:
        if choice != "approve":
            raise ValidationFailed("args can only be modified when approving")
        model = catalog.get(row.tool_name).spec.input_model
        try:
            row.approved_args = model.model_validate(modified_args).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationFailed(
                "modified arguments are invalid",
                details={
                    "errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
                },
            ) from None

    row.status = ApprovalStatus.APPROVED if choice == "approve" else ApprovalStatus.REJECTED
    row.decided_by_user_id = ctx.user_id
    row.decided_at = now
    row.comment = comment
    audit_service.record(
        session,
        event_type=AuditEventType.APPROVAL_APPROVED
        if choice == "approve"
        else AuditEventType.APPROVAL_REJECTED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="approval",
        resource_id=row.id,
        execution_id=row.execution_id,
        metadata={
            "tool": row.tool_name,
            "risk_level": row.risk_level,
            "modified": modified_args is not None,
            "approved_args": sanitize_metadata(row.approved_args) if row.approved_args else None,
            "comment": comment,
        },
    )
    await session.flush()
    await _signal(session, registry, row, actor_user_id=ctx.user_id, request=request)
    await session.commit()
    await session.refresh(row)
    return row


async def _signal(
    session: AsyncSession,
    registry: StepRegistry,
    row: Approval,
    *,
    actor_user_id: uuid.UUID | None,
    request: RequestMeta,
) -> None:
    """Resume the execution waiting on this approval (no-op if it no longer waits)."""
    ex = await session.scalar(
        sa.select(Execution).where(Execution.id == row.execution_id).with_for_update()
    )
    waiting_for = (ex.waiting_on or {}).get("approval_id") if ex is not None else None
    if ex is None or ex.status != ExecutionStatus.WAITING or waiting_for != str(row.id):
        log.info("approval_decided_without_waiting_execution", approval_id=str(row.id))
        return
    decision = {
        ApprovalStatus.APPROVED: "approved",
        ApprovalStatus.REJECTED: "rejected",
        ApprovalStatus.EXPIRED: "expired",
    }.get(row.status, "cancelled")
    # resume_locked commits; the approval update is in the same transaction.
    await resume_locked(
        session,
        registry,
        ex,
        payload={"approval_id": str(row.id), "decision": decision},
        actor_user_id=actor_user_id,
        request=request,
    )


async def expire_due(
    sessionmaker: async_sessionmaker[AsyncSession], registry: StepRegistry, *, limit: int = 100
) -> int:
    """Expire overdue pending approvals and resume their executions (worker loop)."""
    now = utcnow()
    expired = 0
    async with sessionmaker() as s:
        ids = (
            await s.scalars(
                sa.select(Approval.id)
                .where(Approval.status == ApprovalStatus.PENDING, Approval.expires_at <= now)
                .limit(limit)
            )
        ).all()
    for approval_id in ids:
        async with sessionmaker() as s:
            row = await s.scalar(
                sa.select(Approval)
                .where(Approval.id == approval_id)
                .with_for_update(skip_locked=True)
            )
            if row is None or row.status != ApprovalStatus.PENDING:
                continue
            row.status = ApprovalStatus.EXPIRED
            row.decided_at = now
            audit_service.record(
                s,
                event_type=AuditEventType.APPROVAL_EXPIRED,
                request=_SYSTEM,
                organization_id=row.organization_id,
                resource_type="approval",
                resource_id=row.id,
                execution_id=row.execution_id,
                metadata={"tool": row.tool_name},
            )
            await s.flush()
            await _signal(s, registry, row, actor_user_id=None, request=_SYSTEM)
            await s.commit()
            expired += 1
    return expired
