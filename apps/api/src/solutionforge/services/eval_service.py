"""Evaluation datasets and runs, scoring/finalization, comparison, and the deployment gate.

A run executes every case of a dataset against one *pinned* workflow version through the
normal engine (so evaluation measures exactly what production would run: same policy,
tools, budgets, metering). A background finalizer scores a run once every case execution
has ended (or is waiting on a human), or when the run's deadline passes.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.clock import utcnow
from solutionforge.core.errors import (
    Conflict,
    DomainError,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from solutionforge.core.logging import get_logger
from solutionforge.db.tenancy import TenantContext, scoped_select
from solutionforge.domain.approvals import Approval, ApprovalStatus
from solutionforge.domain.audit import AuditEventType
from solutionforge.domain.evaluation import (
    DeploymentDecision,
    EvaluationCase,
    EvaluationDataset,
    EvaluationResult,
    EvaluationRun,
    RunStatus,
)
from solutionforge.domain.tools import ToolCall
from solutionforge.domain.usage import UsageRecord
from solutionforge.domain.workflow import (
    TERMINAL_STATUSES,
    Execution,
    ExecutionStatus,
    ExecutionStep,
    Workflow,
    WorkflowVersion,
)
from solutionforge.evaluation.gate import GatePolicy, evaluate_gate
from solutionforge.evaluation.scorers import (
    CaseExpectations,
    CaseScore,
    GroundedAnswer,
    Observation,
    aggregate,
    score_case,
)
from solutionforge.observability import metrics
from solutionforge.security.rbac import Permission, Role
from solutionforge.services import audit_service
from solutionforge.services.audit_service import RequestMeta
from solutionforge.services.authz import ensure
from solutionforge.services.workflow_service import (
    _deployed_version,
    _workflow,
    get_version,
    new_execution,
)
from solutionforge.workflows.definition import compile_definition, validate_input
from solutionforge.workflows.registry import StepRegistry
from solutionforge.workflows.transitions import terminal_values

log = get_logger(__name__)
DEFAULT_RUN_DEADLINE = timedelta(minutes=15)
# A case whose input the workflow rejects never runs: it fails, with zero measured cost,
# latency and steps (same keys as a scored case so aggregation treats it uniformly).
INVALID_INPUT_SCORES = {
    "status": False,
    "groundedness": None,
    "latency_ms": 0,
    "cost_micro_usd": 0,
    "tokens": 0,
    "steps": 0,
    "error_code": "invalid_input",
}


class DeploymentBlocked(DomainError):
    code = "deployment_blocked"
    status_code = 409


# --------------------------------------------------------------------------- datasets


async def create_dataset(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    workflow_id: uuid.UUID,
    name: str,
    description: str,
    request: RequestMeta,
) -> EvaluationDataset:
    ensure(ctx, Permission.WORKFLOW_WRITE)
    await _workflow(session, ctx, workflow_id)
    ds = EvaluationDataset(
        organization_id=ctx.organization_id,
        workflow_id=workflow_id,
        name=name,
        description=description,
        created_by_user_id=ctx.user_id,
    )
    session.add(ds)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("A dataset with this name already exists for the workflow") from exc
    audit_service.record(
        session,
        event_type=AuditEventType.EVAL_DATASET_CREATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="evaluation_dataset",
        resource_id=ds.id,
        metadata={"name": name},
    )
    await session.commit()
    return ds


async def add_case(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    dataset_id: uuid.UUID,
    name: str,
    input: dict[str, Any],
    expectations: dict[str, Any],
    tags: list[str],
) -> EvaluationCase:
    ensure(ctx, Permission.WORKFLOW_WRITE)
    await get_dataset(session, ctx, dataset_id)
    try:
        exp = CaseExpectations.model_validate(expectations)
    except (PydanticValidationError, ValueError) as exc:
        raise ValidationFailed(
            "invalid case expectations", details={"error": str(exc)[:500]}
        ) from None
    case = EvaluationCase(
        organization_id=ctx.organization_id,
        dataset_id=dataset_id,
        name=name,
        input=input,
        expectations=exp.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        tags=tags,
    )
    session.add(case)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("A case with this name already exists in the dataset") from exc
    return case


async def get_dataset(
    session: AsyncSession, ctx: TenantContext, dataset_id: uuid.UUID
) -> EvaluationDataset:
    ensure(ctx, Permission.EVALUATION_READ)
    ds = await session.scalar(
        scoped_select(EvaluationDataset, ctx, EvaluationDataset.id == dataset_id)
    )
    if ds is None:
        raise NotFound("Dataset not found")
    return ds


async def list_datasets(
    session: AsyncSession, ctx: TenantContext, workflow_id: uuid.UUID | None
) -> list[EvaluationDataset]:
    ensure(ctx, Permission.EVALUATION_READ)
    stmt = scoped_select(EvaluationDataset, ctx)
    if workflow_id is not None:
        stmt = stmt.where(EvaluationDataset.workflow_id == workflow_id)
    return list((await session.scalars(stmt.order_by(EvaluationDataset.name))).all())


async def list_cases(
    session: AsyncSession, ctx: TenantContext, dataset_id: uuid.UUID
) -> list[EvaluationCase]:
    await get_dataset(session, ctx, dataset_id)
    stmt = scoped_select(EvaluationCase, ctx, EvaluationCase.dataset_id == dataset_id)
    return list((await session.scalars(stmt.order_by(EvaluationCase.name))).all())


# --------------------------------------------------------------------------- runs


async def start_run(
    session: AsyncSession,
    ctx: TenantContext,
    registry: StepRegistry,
    *,
    dataset_id: uuid.UUID,
    version: int,
    request: RequestMeta,
) -> EvaluationRun:
    ensure(ctx, Permission.EVALUATION_RUN)
    ds = await get_dataset(session, ctx, dataset_id)
    target = await get_version(session, ctx, ds.workflow_id, version)
    deployed = await _deployed_version(session, ctx, ds.workflow_id)
    if deployed is None or deployed.id != target.id:
        ensure(ctx, Permission.WORKFLOW_WRITE)  # same rule as running unreleased versions
    cases = await list_cases(session, ctx, dataset_id)
    if not cases:
        raise ValidationFailed("dataset has no cases")
    compiled = compile_definition(target.definition, registry)

    run = EvaluationRun(
        organization_id=ctx.organization_id,
        dataset_id=ds.id,
        workflow_id=ds.workflow_id,
        workflow_version_id=target.id,
        version=target.version,
        status=RunStatus.RUNNING,
        deadline_at=utcnow() + DEFAULT_RUN_DEADLINE,
        triggered_by_user_id=ctx.user_id,
    )
    session.add(run)
    await session.flush()
    for case in cases:
        try:
            validate_input(compiled.definition, case.input)
        except ValidationFailed:
            # The input never ran. That is exactly right for a case that expects rejection
            # (status "failed", nothing that requires a run); wrong for every other case.
            exp = CaseExpectations.model_validate(case.expectations)
            expected = (
                exp.status == "failed"
                and not exp.expected_tools
                and exp.output_subset is None
                and exp.output_schema is None
            )
            session.add(
                EvaluationResult(
                    organization_id=ctx.organization_id,
                    run_id=run.id,
                    case_id=case.id,
                    passed=expected,
                    scores={**INVALID_INPUT_SCORES, "status": expected},
                    failures=[]
                    if expected
                    else ["case input does not match the workflow's declared inputs"],
                )
            )
            continue
        session.add(
            new_execution(
                organization_id=ctx.organization_id,
                workflow_id=ds.workflow_id,
                version=target,
                start_step=compiled.definition.start,
                input=case.input,
                created_by_user_id=ctx.user_id,
                evaluation_run_id=run.id,
                evaluation_case_id=case.id,
            )
        )
    audit_service.record(
        session,
        event_type=AuditEventType.EVAL_RUN_STARTED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="evaluation_run",
        resource_id=run.id,
        metadata={"dataset_id": str(ds.id), "version": target.version, "cases": len(cases)},
    )
    await session.commit()
    return run


async def get_run(
    session: AsyncSession, ctx: TenantContext, run_id: uuid.UUID
) -> tuple[EvaluationRun, list[EvaluationResult]]:
    ensure(ctx, Permission.EVALUATION_READ)
    run = await session.scalar(scoped_select(EvaluationRun, ctx, EvaluationRun.id == run_id))
    if run is None:
        raise NotFound("Evaluation run not found")
    results = (
        await session.scalars(
            scoped_select(EvaluationResult, ctx, EvaluationResult.run_id == run_id)
        )
    ).all()
    return run, list(results)


async def list_runs(
    session: AsyncSession, ctx: TenantContext, dataset_id: uuid.UUID
) -> list[EvaluationRun]:
    await get_dataset(session, ctx, dataset_id)
    stmt = scoped_select(EvaluationRun, ctx, EvaluationRun.dataset_id == dataset_id)
    return list((await session.scalars(stmt.order_by(EvaluationRun.created_at.desc()))).all())


async def compare(
    session: AsyncSession, ctx: TenantContext, run_ids: list[uuid.UUID]
) -> dict[str, Any]:
    ensure(ctx, Permission.EVALUATION_READ)
    runs = (
        await session.scalars(scoped_select(EvaluationRun, ctx, EvaluationRun.id.in_(run_ids)))
    ).all()
    if len(runs) != len(set(run_ids)):
        raise NotFound("Evaluation run not found")
    runs = sorted(runs, key=lambda r: r.version)
    results = (
        await session.scalars(
            scoped_select(EvaluationResult, ctx, EvaluationResult.run_id.in_(run_ids))
        )
    ).all()
    cases = {
        c.id: c.name
        for c in (
            await session.scalars(
                scoped_select(
                    EvaluationCase, ctx, EvaluationCase.id.in_({r.case_id for r in results})
                )
            )
        ).all()
    }
    matrix: dict[str, dict[str, bool]] = {}
    for r in results:
        matrix.setdefault(cases.get(r.case_id, str(r.case_id)), {})[str(r.run_id)] = r.passed
    return {
        "runs": [
            {"id": str(r.id), "version": r.version, "status": r.status.value, "metrics": r.metrics}
            for r in runs
        ],
        "cases": matrix,
    }


# --------------------------------------------------------------------------- finalization


async def finalize_due_runs(
    sessionmaker: async_sessionmaker[AsyncSession], *, limit: int = 20
) -> int:
    """Score runs whose case executions have all ended (or are waiting on a human), or whose
    deadline passed. Waiting/stuck executions are then cancelled so they don't linger."""
    done = 0
    now = utcnow()
    async with sessionmaker() as s:
        run_ids = (
            await s.scalars(
                sa.select(EvaluationRun.id)
                .where(EvaluationRun.status == RunStatus.RUNNING)
                .limit(limit)
            )
        ).all()
    for run_id in run_ids:
        async with sessionmaker() as s:
            run = await s.scalar(
                sa.select(EvaluationRun)
                .where(EvaluationRun.id == run_id)
                .with_for_update(skip_locked=True)
            )
            if run is None or run.status != RunStatus.RUNNING:
                continue
            execs = (
                await s.scalars(sa.select(Execution).where(Execution.evaluation_run_id == run.id))
            ).all()
            settled = {*TERMINAL_STATUSES, ExecutionStatus.WAITING}
            if any(e.status not in settled for e in execs) and now < run.deadline_at:
                continue
            await _score_run(s, run, list(execs))
            await s.commit()
            done += 1
    return done


async def _score_run(s: AsyncSession, run: EvaluationRun, execs: list[Execution]) -> None:
    now = utcnow()
    cases = {
        c.id: c
        for c in (
            await s.scalars(
                sa.select(EvaluationCase).where(EvaluationCase.dataset_id == run.dataset_id)
            )
        ).all()
    }
    existing = {
        r.case_id: r
        for r in (
            await s.scalars(sa.select(EvaluationResult).where(EvaluationResult.run_id == run.id))
        ).all()
    }
    scored: list[tuple[CaseScore, list[str]]] = [
        (
            CaseScore(r.passed, r.scores, r.failures),
            cases[r.case_id].tags if r.case_id in cases else [],
        )
        for r in existing.values()
    ]
    for ex in execs:
        case = cases.get(ex.evaluation_case_id) if ex.evaluation_case_id else None
        if case is None or case.id in existing:
            continue
        obs = await _observe(s, ex)
        score = score_case(CaseExpectations.model_validate(case.expectations), obs)
        s.add(
            EvaluationResult(
                organization_id=run.organization_id,
                run_id=run.id,
                case_id=case.id,
                execution_id=ex.id,
                passed=score.passed,
                scores=score.scores,
                failures=score.failures,
            )
        )
        scored.append((score, case.tags))
        if ex.status not in TERMINAL_STATUSES:  # waiting on a human, or past the deadline
            await s.execute(
                sa.update(Execution)
                .where(Execution.id == ex.id)
                .values(
                    **terminal_values(
                        ExecutionStatus.CANCELLED, now, error={"code": "evaluation_finished"}
                    )
                )
            )
            await s.execute(
                sa.update(Approval)
                .where(Approval.execution_id == ex.id, Approval.status == ApprovalStatus.PENDING)
                .values(status=ApprovalStatus.CANCELLED, decided_at=now)
            )
    run.metrics = aggregate(scored)
    run.status = RunStatus.COMPLETED
    run.finished_at = now
    log.info("evaluation_run_completed", run_id=str(run.id), pass_rate=run.metrics.get("pass_rate"))


async def _observe(s: AsyncSession, ex: Execution) -> Observation:
    # "Attempted" = executed, denied, OR held for human approval. An approval request is not a
    # tool_calls row until a human approves, but a workflow that *asked* to issue a refund has
    # attempted it: expected/forbidden tool checks must see it.
    tools = [
        *(await s.scalars(sa.select(ToolCall.tool_name).where(ToolCall.execution_id == ex.id))),
        *(await s.scalars(sa.select(Approval.tool_name).where(Approval.execution_id == ex.id))),
    ]
    cost, tokens = (
        await s.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(UsageRecord.cost_micro_usd), 0),
                sa.func.coalesce(
                    sa.func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0
                ),
            ).where(UsageRecord.execution_id == ex.id)
        )
    ).one()
    steps = (
        await s.scalars(
            sa.select(ExecutionStep)
            .where(ExecutionStep.execution_id == ex.id)
            .order_by(ExecutionStep.seq)
        )
    ).all()
    answers, retrieved, sources = [], set(), []
    for st in steps:
        out = st.output or {}
        if st.step_type == "retrieve" and st.status.value == "succeeded":
            for c in out.get("chunks", []):
                retrieved.add(str(c.get("chunk_id")))
                sources.append(str(c.get("text", "")))
        if st.step_type == "grounded_answer" and st.status.value == "succeeded":
            answers.append(
                GroundedAnswer(
                    answer=str(out.get("answer", "")),
                    insufficient_context=bool(out.get("insufficient_context")),
                    cited_titles=[str(c.get("title")) for c in out.get("citations", [])],
                    cited_chunk_ids=[str(c.get("chunk_id")) for c in out.get("citations", [])],
                )
            )
    return Observation(
        status=ex.status.value,
        output=ex.output,
        error_code=(ex.error or {}).get("code"),
        steps_used=ex.steps_used,
        active_ms=ex.active_ms,
        cost_micro_usd=int(cost),
        tokens=int(tokens),
        tools_attempted=list(tools),
        answers=answers,
        retrieved_chunk_ids=retrieved,
        source_texts=sources,
    )


# --------------------------------------------------------------------------- gate


async def set_gate(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    workflow_id: uuid.UUID,
    policy: GatePolicy | None,
    request: RequestMeta,
) -> Workflow:
    ensure(ctx, Permission.WORKFLOW_DEPLOY)
    wf = await _workflow(session, ctx, workflow_id)
    if policy is not None:
        ds = await get_dataset(session, ctx, policy.dataset_id)
        if ds.workflow_id != workflow_id:
            raise ValidationFailed("gate dataset belongs to a different workflow")
    before = wf.deployment_gate
    wf.deployment_gate = policy.model_dump(mode="json") if policy else None
    audit_service.record(
        session,
        event_type=AuditEventType.GATE_POLICY_UPDATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="workflow",
        resource_id=workflow_id,
        metadata={"before": before, "after": wf.deployment_gate},
    )
    await session.commit()
    return wf


async def enforce_gate(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    workflow: Workflow,
    target: WorkflowVersion,
    current: WorkflowVersion | None,
    override_reason: str | None,
    request: RequestMeta,
) -> DeploymentDecision | None:
    """Called by deploy(). Returns the recorded decision; raises DeploymentBlocked on failure
    unless an OWNER overrides with a reason (break-glass, audited)."""
    if workflow.deployment_gate is None:
        return None
    policy = GatePolicy.model_validate(workflow.deployment_gate)

    async def latest_run(version_id: uuid.UUID) -> EvaluationRun | None:
        return await session.scalar(
            scoped_select(
                EvaluationRun,
                ctx,
                EvaluationRun.dataset_id == policy.dataset_id,
                EvaluationRun.workflow_version_id == version_id,
                EvaluationRun.status == RunStatus.COMPLETED,
            )
            .order_by(EvaluationRun.finished_at.desc())
            .limit(1)
        )

    candidate = await latest_run(target.id)
    baseline = await latest_run(current.id) if current is not None else None
    if candidate is None:
        checks = [
            {
                "name": "evaluated",
                "passed": False,
                "detail": f"no completed evaluation of v{target.version} on the gate dataset",
            }
        ]
    else:
        checks = evaluate_gate(
            policy, candidate.metrics or {}, baseline.metrics if baseline else None
        )
    passed = all(c["passed"] for c in checks)
    overridden = False
    if not passed and override_reason:
        if ctx.role != Role.OWNER:
            raise PermissionDenied("Only an owner can override a failed deployment gate")
        overridden = True
    decision = DeploymentDecision(
        organization_id=ctx.organization_id,
        workflow_id=workflow.id,
        version=target.version,
        candidate_run_id=candidate.id if candidate else None,
        baseline_run_id=baseline.id if baseline else None,
        passed=passed,
        overridden=overridden,
        override_reason=override_reason if overridden else None,
        checks=checks,
        decided_by_user_id=ctx.user_id,
    )
    session.add(decision)
    await session.flush()
    metrics.DEPLOY_DECISIONS.labels(
        "passed" if passed else "overridden" if overridden else "blocked"
    ).inc()
    if not passed and not overridden:
        audit_service.record(
            session,
            event_type=AuditEventType.DEPLOYMENT_BLOCKED,
            request=request,
            actor_user_id=ctx.user_id,
            organization_id=ctx.organization_id,
            resource_type="workflow",
            resource_id=workflow.id,
            metadata={"version": target.version, "checks": checks},
        )
        await session.commit()  # the blocked decision itself is evidence: keep it
        raise DeploymentBlocked(
            f"Deployment of v{target.version} blocked by the quality gate",
            details={"decision_id": str(decision.id), "checks": checks},
        )
    if overridden:
        audit_service.record(
            session,
            event_type=AuditEventType.DEPLOYMENT_GATE_OVERRIDDEN,
            request=request,
            actor_user_id=ctx.user_id,
            organization_id=ctx.organization_id,
            resource_type="workflow",
            resource_id=workflow.id,
            metadata={"version": target.version, "reason": override_reason, "checks": checks},
        )
    return decision


async def list_decisions(
    session: AsyncSession, ctx: TenantContext, workflow_id: uuid.UUID
) -> list[DeploymentDecision]:
    ensure(ctx, Permission.WORKFLOW_READ)
    stmt = scoped_select(DeploymentDecision, ctx, DeploymentDecision.workflow_id == workflow_id)
    return list((await session.scalars(stmt.order_by(DeploymentDecision.created_at.desc()))).all())
