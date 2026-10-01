from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from solutionforge.api.deps import RegistryDep, RequestMetaDep, SessionDep, SettingsDep, TenantDep
from solutionforge.domain.workflow import Execution, ExecutionStatus, ExecutionStep
from solutionforge.llm.pricing import micro_to_usd
from solutionforge.schemas.workflow import (
    CreateExecutionRequest,
    CreateVersionRequest,
    CreateWorkflowRequest,
    DeploymentOut,
    DeployRequest,
    ExecutionDetailOut,
    ExecutionOut,
    ExecutionStepOut,
    LLMUsageOut,
    ResumeRequest,
    VersionOut,
    WorkflowOut,
)
from solutionforge.services import usage_service
from solutionforge.services import workflow_service as svc

router = APIRouter(prefix="/orgs/{org_id}", tags=["workflows"])


def _wf_out(s: svc.WorkflowSummary) -> WorkflowOut:
    return WorkflowOut(
        id=s.workflow.id,
        name=s.workflow.name,
        description=s.workflow.description,
        latest_version=s.latest_version,
        deployed_version=s.deployed_version,
        created_at=s.workflow.created_at,
    )


async def _detail(
    session: SessionDep, ex: Execution, steps: list[ExecutionStep]
) -> ExecutionDetailOut:
    calls, tokens, cost = await usage_service.execution_totals(session, ex.organization_id, ex.id)
    return ExecutionDetailOut(
        **ExecutionOut.model_validate(ex).model_dump(),
        steps=[ExecutionStepOut.model_validate(s) for s in steps],
        llm_usage=LLMUsageOut(calls=calls, tokens=tokens, cost_usd=micro_to_usd(cost)),
    )


# --------------------------------------------------------------------------- workflows


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    body: CreateWorkflowRequest, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> WorkflowOut:
    wf = await svc.create_workflow(
        session, ctx, name=body.name, description=body.description, request=meta
    )
    return _wf_out(svc.WorkflowSummary(wf, None, None))


@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(session: SessionDep, ctx: TenantDep) -> list[WorkflowOut]:
    return [_wf_out(s) for s in await svc.list_workflows(session, ctx)]


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(workflow_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> WorkflowOut:
    return _wf_out(await svc.get_workflow(session, ctx, workflow_id))


# --------------------------------------------------------------------------- versions


@router.post(
    "/workflows/{workflow_id}/versions",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    workflow_id: uuid.UUID,
    body: CreateVersionRequest,
    session: SessionDep,
    ctx: TenantDep,
    registry: RegistryDep,
    meta: RequestMetaDep,
) -> VersionOut:
    v = await svc.create_version(
        session,
        ctx,
        registry,
        workflow_id=workflow_id,
        definition=body.definition,
        changelog=body.changelog,
        request=meta,
    )
    return VersionOut.model_validate(v)


@router.get("/workflows/{workflow_id}/versions", response_model=list[VersionOut])
async def list_versions(
    workflow_id: uuid.UUID, session: SessionDep, ctx: TenantDep
) -> list[VersionOut]:
    return [
        VersionOut.model_validate(v) for v in await svc.list_versions(session, ctx, workflow_id)
    ]


@router.get("/workflows/{workflow_id}/versions/{version}", response_model=VersionOut)
async def get_version(
    workflow_id: uuid.UUID, version: int, session: SessionDep, ctx: TenantDep
) -> VersionOut:
    return VersionOut.model_validate(await svc.get_version(session, ctx, workflow_id, version))


# --------------------------------------------------------------------------- deployments


@router.post(
    "/workflows/{workflow_id}/deployments",
    response_model=DeploymentOut,
    status_code=status.HTTP_201_CREATED,
)
async def deploy(
    workflow_id: uuid.UUID,
    body: DeployRequest,
    session: SessionDep,
    ctx: TenantDep,
    meta: RequestMetaDep,
) -> DeploymentOut:
    dep = await svc.deploy(
        session,
        ctx,
        workflow_id=workflow_id,
        version=body.version,
        reason=body.reason,
        request=meta,
        override_reason=body.override_gate_reason,
    )
    return DeploymentOut(
        id=dep.id,
        version=body.version,
        reason=dep.reason,
        deployed_by_user_id=dep.deployed_by_user_id,
        created_at=dep.created_at,
    )


@router.get("/workflows/{workflow_id}/deployments", response_model=list[DeploymentOut])
async def list_deployments(
    workflow_id: uuid.UUID, session: SessionDep, ctx: TenantDep
) -> list[DeploymentOut]:
    return [
        DeploymentOut(
            id=d.id,
            version=v,
            reason=d.reason,
            deployed_by_user_id=d.deployed_by_user_id,
            created_at=d.created_at,
        )
        for d, v in await svc.list_deployments(session, ctx, workflow_id)
    ]


# --------------------------------------------------------------------------- executions


@router.post(
    "/workflows/{workflow_id}/executions",
    response_model=ExecutionOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "Idempotent replay: the existing execution"}},
)
async def create_execution(
    workflow_id: uuid.UUID,
    body: CreateExecutionRequest,
    response: Response,
    session: SessionDep,
    ctx: TenantDep,
    registry: RegistryDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> ExecutionOut:
    ex, created = await svc.create_execution(
        session,
        ctx,
        registry,
        workflow_id=workflow_id,
        input=body.input,
        version=body.version,
        idempotency_key=body.idempotency_key,
        max_input_bytes=settings.max_execution_input_bytes,
        request=meta,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return ExecutionOut.model_validate(ex)


@router.get("/executions", response_model=list[ExecutionOut])
async def list_executions(
    session: SessionDep,
    ctx: TenantDep,
    workflow_id: uuid.UUID | None = None,
    status_: Annotated[ExecutionStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: datetime | None = None,
) -> list[ExecutionOut]:
    rows = await svc.list_executions(
        session, ctx, workflow_id=workflow_id, status=status_, limit=limit, before=before
    )
    return [ExecutionOut.model_validate(e) for e in rows]


@router.get("/executions/{execution_id}", response_model=ExecutionDetailOut)
async def get_execution(
    execution_id: uuid.UUID, session: SessionDep, ctx: TenantDep
) -> ExecutionDetailOut:
    ex, steps = await svc.get_execution(session, ctx, execution_id)
    return await _detail(session, ex, steps)


@router.post("/executions/{execution_id}/cancel", response_model=ExecutionOut)
async def cancel_execution(
    execution_id: uuid.UUID, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> ExecutionOut:
    ex = await svc.cancel_execution(session, ctx, execution_id=execution_id, request=meta)
    return ExecutionOut.model_validate(ex)


@router.post("/executions/{execution_id}/resume", response_model=ExecutionOut)
async def resume_execution(
    execution_id: uuid.UUID,
    body: ResumeRequest,
    session: SessionDep,
    ctx: TenantDep,
    registry: RegistryDep,
    meta: RequestMetaDep,
) -> ExecutionOut:
    ex = await svc.resume_execution(
        session, ctx, registry, execution_id=execution_id, payload=body.payload, request=meta
    )
    return ExecutionOut.model_validate(ex)
