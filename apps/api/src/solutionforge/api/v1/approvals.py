"""Approval inbox and decisions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from solutionforge.api.deps import RegistryDep, RequestMetaDep, SessionDep, TenantDep
from solutionforge.domain.approvals import Approval, ApprovalStatus
from solutionforge.services import approval_service as svc
from solutionforge.tools.executor import ToolExecutor

router = APIRouter(prefix="/orgs/{org_id}", tags=["approvals"])


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    execution_id: uuid.UUID
    step_id: str
    tool_name: str
    risk_level: str
    reason: str
    required_permission: str
    proposed_args: dict[str, Any]
    approved_args: dict[str, Any] | None
    status: ApprovalStatus
    requested_by_user_id: uuid.UUID | None
    decided_by_user_id: uuid.UUID | None
    decided_at: datetime | None
    comment: str | None
    expires_at: datetime
    created_at: datetime


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    args: dict[str, Any] | None = Field(
        default=None, description="Approve with modified arguments (re-validated)"
    )
    comment: str | None = Field(default=None, max_length=2000)


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    session: SessionDep,
    ctx: TenantDep,
    status: ApprovalStatus | None = None,
    execution_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: datetime | None = None,
) -> list[Approval]:
    return await svc.list_approvals(
        session, ctx, status=status, execution_id=execution_id, limit=limit, before=before
    )


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
async def get_approval(approval_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> Approval:
    return await svc.get_approval(session, ctx, approval_id)


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalOut)
async def decide(
    approval_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    session: SessionDep,
    ctx: TenantDep,
    registry: RegistryDep,
    meta: RequestMetaDep,
) -> Approval:
    executor: ToolExecutor = request.app.state.tool_executor
    return await svc.decide(
        session,
        ctx,
        registry,
        executor.catalog,
        approval_id=approval_id,
        choice=body.decision,
        modified_args=body.args,
        comment=body.comment,
        request=meta,
    )
