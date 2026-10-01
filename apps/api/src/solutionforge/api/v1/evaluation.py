from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from solutionforge.api.deps import RegistryDep, RequestMetaDep, SessionDep, TenantDep
from solutionforge.evaluation.gate import GatePolicy
from solutionforge.schemas.evaluation import (
    CaseOut,
    CreateCaseRequest,
    CreateDatasetRequest,
    DatasetOut,
    DecisionOut,
    ResultOut,
    RunDetailOut,
    RunOut,
    StartRunRequest,
)
from solutionforge.services import eval_service as svc
from solutionforge.services.workflow_service import get_workflow

router = APIRouter(prefix="/orgs/{org_id}", tags=["evaluation"])


@router.post("/evaluation/datasets", response_model=DatasetOut, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    body: CreateDatasetRequest, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> DatasetOut:
    ds = await svc.create_dataset(
        session,
        ctx,
        workflow_id=body.workflow_id,
        name=body.name,
        description=body.description,
        request=meta,
    )
    return DatasetOut.model_validate(ds)


@router.get("/evaluation/datasets", response_model=list[DatasetOut])
async def list_datasets(
    session: SessionDep,
    ctx: TenantDep,
    workflow_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[DatasetOut]:
    datasets = await svc.list_datasets(session, ctx, workflow_id)
    return [DatasetOut.model_validate(d) for d in datasets]


@router.post(
    "/evaluation/datasets/{dataset_id}/cases",
    response_model=CaseOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_case(
    dataset_id: uuid.UUID, body: CreateCaseRequest, session: SessionDep, ctx: TenantDep
) -> CaseOut:
    case = await svc.add_case(
        session,
        ctx,
        dataset_id=dataset_id,
        name=body.name,
        input=body.input,
        expectations=body.expectations,
        tags=body.tags,
    )
    return CaseOut.model_validate(case)


@router.get("/evaluation/datasets/{dataset_id}/cases", response_model=list[CaseOut])
async def list_cases(dataset_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> list[CaseOut]:
    return [CaseOut.model_validate(c) for c in await svc.list_cases(session, ctx, dataset_id)]


@router.post(
    "/evaluation/datasets/{dataset_id}/runs",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_run(
    dataset_id: uuid.UUID,
    body: StartRunRequest,
    session: SessionDep,
    ctx: TenantDep,
    registry: RegistryDep,
    meta: RequestMetaDep,
) -> RunOut:
    run = await svc.start_run(
        session, ctx, registry, dataset_id=dataset_id, version=body.version, request=meta
    )
    return RunOut.model_validate(run)


@router.get("/evaluation/datasets/{dataset_id}/runs", response_model=list[RunOut])
async def list_runs(dataset_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> list[RunOut]:
    return [RunOut.model_validate(r) for r in await svc.list_runs(session, ctx, dataset_id)]


@router.get("/evaluation/runs/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> RunDetailOut:
    run, results = await svc.get_run(session, ctx, run_id)
    return RunDetailOut(
        **RunOut.model_validate(run).model_dump(),
        results=[ResultOut.model_validate(r) for r in results],
    )


@router.get("/evaluation/compare")
async def compare(
    session: SessionDep,
    ctx: TenantDep,
    run_id: Annotated[list[uuid.UUID], Query(min_length=1, max_length=5)],
) -> dict[str, Any]:
    return await svc.compare(session, ctx, run_id)


@router.get("/workflows/{workflow_id}/gate")
async def get_gate(workflow_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> dict[str, Any]:
    summary = await get_workflow(session, ctx, workflow_id)
    return {"policy": summary.workflow.deployment_gate}


@router.put("/workflows/{workflow_id}/gate")
async def set_gate(
    workflow_id: uuid.UUID,
    body: GatePolicy,
    session: SessionDep,
    ctx: TenantDep,
    meta: RequestMetaDep,
) -> dict[str, Any]:
    wf = await svc.set_gate(session, ctx, workflow_id=workflow_id, policy=body, request=meta)
    return {"policy": wf.deployment_gate}


@router.delete("/workflows/{workflow_id}/gate")
async def delete_gate(
    workflow_id: uuid.UUID, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> dict[str, Any]:
    await svc.set_gate(session, ctx, workflow_id=workflow_id, policy=None, request=meta)
    return {"policy": None}


@router.get("/workflows/{workflow_id}/deployment-decisions", response_model=list[DecisionOut])
async def list_decisions(
    workflow_id: uuid.UUID, session: SessionDep, ctx: TenantDep
) -> list[DecisionOut]:
    decisions = await svc.list_decisions(session, ctx, workflow_id)
    return [DecisionOut.model_validate(d) for d in decisions]
