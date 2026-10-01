"""Workflows, immutable versions, deployments, and execution lifecycle (create/cancel/resume).

Execution *running* happens in the engine (worker process). This module only creates,
inspects and signals executions, always within a TenantContext.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.core.errors import Conflict, NotFound, ValidationFailed
from solutionforge.db.tenancy import TenantContext, scoped_select
from solutionforge.domain.approvals import Approval, ApprovalStatus
from solutionforge.domain.audit import AuditEventType
from solutionforge.domain.workflow import (
    Execution,
    ExecutionStatus,
    ExecutionStep,
    StepStatus,
    Workflow,
    WorkflowDeployment,
    WorkflowVersion,
)
from solutionforge.security.rbac import Permission
from solutionforge.services import audit_service
from solutionforge.services.audit_service import RequestMeta
from solutionforge.services.authz import ensure
from solutionforge.workflows import transitions
from solutionforge.workflows.definition import compile_definition, validate_input
from solutionforge.workflows.registry import StepContext, StepError, StepRegistry

# --------------------------------------------------------------------------- workflows


async def create_workflow(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    name: str,
    description: str,
    request: RequestMeta,
) -> Workflow:
    ensure(ctx, Permission.WORKFLOW_WRITE)
    wf = Workflow(
        organization_id=ctx.organization_id,
        name=name,
        description=description,
        created_by_user_id=ctx.user_id,
    )
    session.add(wf)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("A workflow with this name already exists") from exc
    audit_service.record(
        session,
        event_type=AuditEventType.WORKFLOW_CREATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="workflow",
        resource_id=wf.id,
        metadata={"name": name},
    )
    await session.commit()
    return wf


@dataclass(frozen=True, slots=True)
class WorkflowSummary:
    workflow: Workflow
    latest_version: int | None
    deployed_version: int | None


async def list_workflows(session: AsyncSession, ctx: TenantContext) -> list[WorkflowSummary]:
    ensure(ctx, Permission.WORKFLOW_READ)
    wfs = (await session.scalars(scoped_select(Workflow, ctx).order_by(Workflow.name))).all()
    return [await _summary(session, ctx, wf) for wf in wfs]


async def get_workflow(
    session: AsyncSession, ctx: TenantContext, workflow_id: uuid.UUID
) -> WorkflowSummary:
    ensure(ctx, Permission.WORKFLOW_READ)
    return await _summary(session, ctx, await _workflow(session, ctx, workflow_id))


async def _summary(session: AsyncSession, ctx: TenantContext, wf: Workflow) -> WorkflowSummary:
    latest = await session.scalar(
        sa.select(sa.func.max(WorkflowVersion.version)).where(
            WorkflowVersion.organization_id == ctx.organization_id,
            WorkflowVersion.workflow_id == wf.id,
        )
    )
    deployed = await _deployed_version(session, ctx, wf.id)
    return WorkflowSummary(wf, latest, deployed.version if deployed else None)


async def _workflow(session: AsyncSession, ctx: TenantContext, workflow_id: uuid.UUID) -> Workflow:
    wf = await session.scalar(scoped_select(Workflow, ctx, Workflow.id == workflow_id))
    if wf is None:
        raise NotFound("Workflow not found")
    return wf


# --------------------------------------------------------------------------- versions


async def create_version(
    session: AsyncSession,
    ctx: TenantContext,
    registry: StepRegistry,
    *,
    workflow_id: uuid.UUID,
    definition: dict[str, Any],
    changelog: str,
    request: RequestMeta,
) -> WorkflowVersion:
    ensure(ctx, Permission.WORKFLOW_WRITE)
    await _workflow(session, ctx, workflow_id)
    compiled = compile_definition(definition, registry)  # raises DefinitionInvalid (422)

    current = await session.scalar(
        sa.select(sa.func.max(WorkflowVersion.version)).where(
            WorkflowVersion.organization_id == ctx.organization_id,
            WorkflowVersion.workflow_id == workflow_id,
        )
    )
    version = WorkflowVersion(
        organization_id=ctx.organization_id,
        workflow_id=workflow_id,
        version=(current or 0) + 1,
        definition=compiled.canonical_json(),
        definition_hash=compiled.hash(),
        changelog=changelog,
        created_by_user_id=ctx.user_id,
    )
    session.add(version)
    try:
        await session.flush()
    except IntegrityError as exc:  # two concurrent creates picked the same number
        await session.rollback()
        raise Conflict("Concurrent version creation; retry") from exc
    audit_service.record(
        session,
        event_type=AuditEventType.WORKFLOW_VERSION_CREATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="workflow_version",
        resource_id=version.id,
        metadata={
            "workflow_id": str(workflow_id),
            "version": version.version,
            "definition_hash": version.definition_hash,
        },
    )
    await session.commit()
    return version


async def list_versions(
    session: AsyncSession, ctx: TenantContext, workflow_id: uuid.UUID
) -> list[WorkflowVersion]:
    ensure(ctx, Permission.WORKFLOW_READ)
    await _workflow(session, ctx, workflow_id)
    stmt = scoped_select(WorkflowVersion, ctx, WorkflowVersion.workflow_id == workflow_id)
    return list((await session.scalars(stmt.order_by(WorkflowVersion.version.desc()))).all())


async def get_version(
    session: AsyncSession, ctx: TenantContext, workflow_id: uuid.UUID, version: int
) -> WorkflowVersion:
    ensure(ctx, Permission.WORKFLOW_READ)
    v = await session.scalar(
        scoped_select(
            WorkflowVersion,
            ctx,
            WorkflowVersion.workflow_id == workflow_id,
            WorkflowVersion.version == version,
        )
    )
    if v is None:
        raise NotFound("Workflow version not found")
    return v


# --------------------------------------------------------------------------- deployments


async def deploy(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    workflow_id: uuid.UUID,
    version: int,
    reason: str,
    request: RequestMeta,
    override_reason: str | None = None,
) -> WorkflowDeployment:
    """Make ``version`` the production version. Deploying an older version is a rollback.

    If the workflow has a deployment gate, the version must pass it (see
    ``eval_service.enforce_gate``); rollbacks are gated too — "known good" is
    re-proven by evaluation, not assumed.
    """
    from solutionforge.services.eval_service import enforce_gate  # import cycle

    ensure(ctx, Permission.WORKFLOW_DEPLOY)
    wf = await _workflow(session, ctx, workflow_id)
    target = await get_version(session, ctx, workflow_id, version)
    current = await _deployed_version(session, ctx, workflow_id)
    if current is not None and current.id == target.id:
        raise Conflict(f"Version {version} is already deployed")
    decision = await enforce_gate(
        session,
        ctx,
        workflow=wf,
        target=target,
        current=current,
        override_reason=override_reason,
        request=request,
    )
    dep = WorkflowDeployment(
        organization_id=ctx.organization_id,
        workflow_id=workflow_id,
        workflow_version_id=target.id,
        deployed_by_user_id=ctx.user_id,
        reason=reason,
    )
    session.add(dep)
    await session.flush()
    audit_service.record(
        session,
        event_type=AuditEventType.WORKFLOW_DEPLOYED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="workflow",
        resource_id=workflow_id,
        metadata={
            "version": version,
            "previous_version": current.version if current else None,
            "reason": reason,
            "gate_decision_id": str(decision.id) if decision else None,
        },
    )
    await session.commit()
    return dep


async def list_deployments(
    session: AsyncSession, ctx: TenantContext, workflow_id: uuid.UUID
) -> list[tuple[WorkflowDeployment, int]]:
    ensure(ctx, Permission.WORKFLOW_READ)
    await _workflow(session, ctx, workflow_id)
    rows = await session.execute(
        sa.select(WorkflowDeployment, WorkflowVersion.version)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowDeployment.workflow_version_id)
        .where(
            WorkflowDeployment.organization_id == ctx.organization_id,
            WorkflowDeployment.workflow_id == workflow_id,
        )
        .order_by(WorkflowDeployment.created_at.desc(), WorkflowDeployment.id.desc())
    )
    return [(d, v) for d, v in rows]


async def _deployed_version(
    session: AsyncSession, ctx: TenantContext, workflow_id: uuid.UUID
) -> WorkflowVersion | None:
    return await session.scalar(
        sa.select(WorkflowVersion)
        .join(WorkflowDeployment, WorkflowDeployment.workflow_version_id == WorkflowVersion.id)
        .where(
            WorkflowDeployment.organization_id == ctx.organization_id,
            WorkflowDeployment.workflow_id == workflow_id,
        )
        .order_by(WorkflowDeployment.created_at.desc(), WorkflowDeployment.id.desc())
        .limit(1)
    )


# --------------------------------------------------------------------------- executions


async def create_execution(
    session: AsyncSession,
    ctx: TenantContext,
    registry: StepRegistry,
    *,
    workflow_id: uuid.UUID,
    input: dict[str, Any],
    version: int | None,
    idempotency_key: str | None,
    max_input_bytes: int,
    request: RequestMeta,
) -> tuple[Execution, bool]:
    """Queue an execution. Returns (execution, created). With an idempotency key, a repeat
    request returns the original execution instead of starting a second one."""
    ensure(ctx, Permission.WORKFLOW_EXECUTE)
    await _workflow(session, ctx, workflow_id)

    if idempotency_key is not None:
        existing = await _by_idempotency_key(session, ctx, idempotency_key)
        if existing is not None:
            if existing.workflow_id != workflow_id:
                raise Conflict("Idempotency key already used for a different workflow")
            return existing, False

    if version is None:
        target = await _deployed_version(session, ctx, workflow_id)
        if target is None:
            raise Conflict("Workflow has no deployed version; deploy one or pass 'version'")
    else:
        target = await get_version(session, ctx, workflow_id, version)
        deployed = await _deployed_version(session, ctx, workflow_id)
        if deployed is None or deployed.id != target.id:
            # Running unreleased versions is a development action, not an operator one.
            ensure(ctx, Permission.WORKFLOW_WRITE)

    if len(json.dumps(input, separators=(",", ":")).encode()) > max_input_bytes:
        raise ValidationFailed(f"Execution input exceeds {max_input_bytes} bytes")
    compiled = compile_definition(target.definition, registry)
    validate_input(compiled.definition, input)

    ex = new_execution(
        organization_id=ctx.organization_id,
        workflow_id=workflow_id,
        version=target,
        start_step=compiled.definition.start,
        input=input,
        created_by_user_id=ctx.user_id,
        idempotency_key=idempotency_key,
    )
    session.add(ex)
    try:
        await session.flush()
    except IntegrityError:  # concurrent request with the same idempotency key won
        await session.rollback()
        assert idempotency_key is not None
        existing = await _by_idempotency_key(session, ctx, idempotency_key)
        if existing is None or existing.workflow_id != workflow_id:
            raise Conflict("Idempotency key conflict") from None
        return existing, False
    audit_service.record(
        session,
        event_type=AuditEventType.EXECUTION_CREATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="execution",
        resource_id=ex.id,
        execution_id=ex.id,
        metadata={"workflow_id": str(workflow_id), "version": target.version},
    )
    await session.commit()
    return ex, True


def new_execution(
    *,
    organization_id: uuid.UUID,
    workflow_id: uuid.UUID,
    version: WorkflowVersion,
    start_step: str,
    input: dict[str, Any],
    created_by_user_id: uuid.UUID | None,
    idempotency_key: str | None = None,
    evaluation_run_id: uuid.UUID | None = None,
    evaluation_case_id: uuid.UUID | None = None,
) -> Execution:
    """A queued execution of a pinned version. Callers validate input and authorize."""
    return Execution(
        organization_id=organization_id,
        workflow_id=workflow_id,
        workflow_version_id=version.id,
        status=ExecutionStatus.QUEUED,
        idempotency_key=idempotency_key,
        created_by_user_id=created_by_user_id,
        input=input,
        state={},
        step_outputs={},
        current_step=start_step,
        run_after=utcnow(),
        evaluation_run_id=evaluation_run_id,
        evaluation_case_id=evaluation_case_id,
    )


async def _by_idempotency_key(
    session: AsyncSession, ctx: TenantContext, key: str
) -> Execution | None:
    return await session.scalar(scoped_select(Execution, ctx, Execution.idempotency_key == key))


async def list_executions(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    workflow_id: uuid.UUID | None,
    status: ExecutionStatus | None,
    limit: int,
    before: datetime | None,
) -> list[Execution]:
    ensure(ctx, Permission.WORKFLOW_READ)
    stmt = scoped_select(Execution, ctx)
    if workflow_id is not None:
        stmt = stmt.where(Execution.workflow_id == workflow_id)
    if status is not None:
        stmt = stmt.where(Execution.status == status)
    if before is not None:
        stmt = stmt.where(Execution.created_at < before)
    stmt = stmt.order_by(Execution.created_at.desc(), Execution.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def get_execution(
    session: AsyncSession, ctx: TenantContext, execution_id: uuid.UUID
) -> tuple[Execution, list[ExecutionStep]]:
    ensure(ctx, Permission.WORKFLOW_READ)
    ex = await session.scalar(scoped_select(Execution, ctx, Execution.id == execution_id))
    if ex is None:
        raise NotFound("Execution not found")
    steps = (
        await session.scalars(
            scoped_select(ExecutionStep, ctx, ExecutionStep.execution_id == execution_id).order_by(
                ExecutionStep.seq
            )
        )
    ).all()
    return ex, list(steps)


async def cancel_execution(
    session: AsyncSession, ctx: TenantContext, *, execution_id: uuid.UUID, request: RequestMeta
) -> Execution:
    """Queued/waiting executions stop immediately; running ones stop before their next step."""
    ensure(ctx, Permission.WORKFLOW_EXECUTE)
    now = utcnow()
    scope = (Execution.id == execution_id, Execution.organization_id == ctx.organization_id)
    err = {"code": "cancelled", "message": "Cancelled by user", "by": str(ctx.user_id)}
    stopped = await session.execute(
        sa.update(Execution)
        .where(*scope, Execution.status.in_([ExecutionStatus.QUEUED, ExecutionStatus.WAITING]))
        .values(**transitions.terminal_values(ExecutionStatus.CANCELLED, now, error=err))
        .execution_options(synchronize_session=False)
    )
    if stopped.rowcount != 1:  # type: ignore[attr-defined]
        flagged = await session.execute(
            sa.update(Execution)
            .where(*scope, Execution.status == ExecutionStatus.RUNNING)
            .values(cancel_requested=True, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if flagged.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            ex, _ = await get_execution(session, ctx, execution_id)  # 404 if foreign/missing
            raise Conflict(f"Execution is already {ex.status.value}")
    await session.execute(
        sa.update(Approval)
        .where(
            Approval.execution_id == execution_id,
            Approval.organization_id == ctx.organization_id,
            Approval.status == ApprovalStatus.PENDING,
        )
        .values(status=ApprovalStatus.CANCELLED, decided_at=now, decided_by_user_id=ctx.user_id)
        .execution_options(synchronize_session=False)
    )
    audit_service.record(
        session,
        event_type=AuditEventType.EXECUTION_CANCELLED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="execution",
        resource_id=execution_id,
        execution_id=execution_id,
    )
    await session.commit()
    ex, _ = await get_execution(session, ctx, execution_id)
    return ex


async def resume_execution(
    session: AsyncSession,
    ctx: TenantContext,
    registry: StepRegistry,
    *,
    execution_id: uuid.UUID,
    payload: dict[str, Any],
    request: RequestMeta,
) -> Execution:
    """Deliver the signal a WAITING execution is suspended on (``approval`` checkpoints).

    Tool approvals must go through ``POST /approvals/{id}/decision``, which enforces the
    approver permission and four-eyes rule; this endpoint refuses them so it can't be used
    to bypass those checks.
    """
    ensure(ctx, Permission.APPROVAL_DECIDE)
    ex = await session.scalar(
        scoped_select(Execution, ctx, Execution.id == execution_id).with_for_update()
    )
    if ex is None:
        raise NotFound("Execution not found")
    if ex.status != ExecutionStatus.WAITING:
        raise Conflict(f"Execution is {ex.status.value}, not waiting")
    if (ex.waiting_on or {}).get("reason") == "tool_approval":
        raise Conflict("This execution awaits a tool approval; decide it via the approvals API")
    return await resume_locked(
        session, registry, ex, payload=payload, actor_user_id=ctx.user_id, request=request
    )


async def resume_locked(
    session: AsyncSession,
    registry: StepRegistry,
    ex: Execution,
    *,
    payload: dict[str, Any],
    actor_user_id: uuid.UUID | None,
    request: RequestMeta,
) -> Execution:
    """Resume a WAITING execution already loaded ``FOR UPDATE`` by an authorized caller."""
    if ex.status != ExecutionStatus.WAITING:
        raise Conflict(f"Execution is {ex.status.value}, not waiting")

    version = await session.get(WorkflowVersion, ex.workflow_version_id)
    assert version is not None  # FK guarantees it
    compiled = compile_definition(version.definition, registry)
    assert ex.current_step is not None
    step = compiled.steps[ex.current_step]
    snap = transitions.Snapshot.of(ex.input, ex.state, ex.step_outputs)
    step_ctx = StepContext(
        organization_id=ex.organization_id,
        execution_id=ex.id,
        workflow_id=ex.workflow_id,
        step_id=step.spec.id,
        attempt=ex.current_attempt,
        scope=snap.handler_scope(),
        deadline=time.monotonic() + step.spec.timeout_seconds,
        visit=ex.visit_seq,
    )
    now = utcnow()
    try:
        result = await step.handler.resume(step.config, step_ctx, payload)
        tr = transitions.on_success(
            snap,
            compiled,
            step,
            result,
            now=now,
            attempt=ex.current_attempt,
            continue_status=ExecutionStatus.QUEUED,
            step_status=StepStatus.RESUMED,
        )
    except PydanticValidationError as exc:
        raise ValidationFailed(
            "Invalid resume payload",
            details={"errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]},
        ) from None
    except StepError as err:
        tr = transitions.on_failure(
            snap,
            step,
            err,
            attempt=ex.current_attempt,
            now=now,
            continue_status=ExecutionStatus.QUEUED,
            step_status=StepStatus.RESUMED,
            final=True,
        )

    values = dict(tr.values) | {"event_seq": Execution.event_seq + 1}
    seq = await session.scalar(
        sa.update(Execution)
        .where(Execution.id == ex.id, Execution.status == ExecutionStatus.WAITING)
        .values(**values)
        .returning(Execution.event_seq)
        .execution_options(synchronize_session=False)
    )
    if seq is None:
        await session.rollback()
        raise Conflict("Execution is no longer waiting")
    session.add(
        ExecutionStep(
            organization_id=ex.organization_id,
            execution_id=ex.id,
            seq=seq,
            step_id=step.spec.id,
            step_type=step.spec.type,
            attempt=ex.current_attempt,
            status=tr.step_status,
            worker_id=None,
            output=tr.step_output,
            error=tr.step_error,
            started_at=now,
            finished_at=now,
            duration_ms=0,
        )
    )
    audit_service.record(
        session,
        event_type=AuditEventType.EXECUTION_RESUMED,
        request=request,
        actor_user_id=actor_user_id,
        organization_id=ex.organization_id,
        resource_type="execution",
        resource_id=ex.id,
        execution_id=ex.id,
        metadata={"step_id": step.spec.id, "payload": payload},
    )
    await session.commit()
    await session.refresh(ex)
    return ex
