"""Tool catalog (MCP-compatible descriptors), installations, call trail, demo data."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from solutionforge.api.deps import RequestMetaDep, SessionDep, TenantDep
from solutionforge.domain.tools import ToolInstallation
from solutionforge.services import tool_service
from solutionforge.tools.executor import ToolExecutor
from solutionforge.tools.policy import ToolPolicyConfig
from solutionforge.tools.spec import TOOL_NAME_PATTERN, ToolSpec

router = APIRouter(prefix="/orgs/{org_id}", tags=["tools"])

ToolName = Annotated[str, Path(pattern=TOOL_NAME_PATTERN)]
ConfigValue = Annotated[str, StringConstraints(max_length=500)]
SecretValue = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


def _executor(request: Request) -> ToolExecutor:
    return request.app.state.tool_executor  # type: ignore[no-any-return]


class InstallationOut(BaseModel):
    enabled: bool
    auto_approve_low_risk: bool
    config: dict[str, Any]
    has_credentials: bool  # credentials themselves are write-only


class ToolOut(BaseModel):
    name: str
    description: str
    risk_level: str
    required_permission: str
    requires_credentials: bool
    config_keys: list[str]
    mcp: dict[str, Any] = Field(description="MCP tools/list descriptor")
    installation: InstallationOut | None


class InstallationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    auto_approve_low_risk: bool = True
    config: dict[str, ConfigValue] = Field(default_factory=dict, max_length=20)
    credentials: dict[str, SecretValue] | None = Field(
        default=None, max_length=20, description="Omit to keep, null to clear, object to replace"
    )


class ToolCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool_name: str
    risk_level: str
    status: str
    execution_id: uuid.UUID | None
    step_id: str | None
    idempotency_key: str | None
    args: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    attempts: int
    latency_ms: int
    created_at: datetime
    finished_at: datetime | None


def _installation_out(i: ToolInstallation | None) -> InstallationOut | None:
    if i is None:
        return None
    return InstallationOut(
        enabled=i.enabled,
        auto_approve_low_risk=i.auto_approve_low_risk,
        config=i.config,
        has_credentials=i.credentials_encrypted is not None,
    )


def _tool_out(spec: ToolSpec, inst: ToolInstallation | None) -> ToolOut:
    return ToolOut(
        name=spec.name,
        description=spec.description,
        risk_level=spec.risk_level.value,
        required_permission=spec.required_permission.value,
        requires_credentials=spec.requires_credentials,
        config_keys=list(spec.config_keys),
        mcp=spec.mcp_descriptor(),
        installation=_installation_out(inst),
    )


@router.get("/tools", response_model=list[ToolOut])
async def list_tools(request: Request, session: SessionDep, ctx: TenantDep) -> list[ToolOut]:
    rows = await tool_service.list_catalog(session, ctx, _executor(request).catalog)
    return [_tool_out(s, i) for s, i in rows]


@router.put("/tools/{tool_name}", response_model=ToolOut)
async def put_installation(
    tool_name: ToolName,
    body: InstallationIn,
    request: Request,
    session: SessionDep,
    ctx: TenantDep,
    meta: RequestMetaDep,
) -> ToolOut:
    executor = _executor(request)
    kwargs: dict[str, Any] = {}
    if "credentials" in body.model_fields_set:
        kwargs["credentials"] = body.credentials
    inst = await tool_service.upsert_installation(
        session,
        ctx,
        executor.catalog,
        executor.cipher,
        tool_name=tool_name,
        enabled=body.enabled,
        auto_approve_low_risk=body.auto_approve_low_risk,
        config=body.config,
        request=meta,
        **kwargs,
    )
    return _tool_out(executor.catalog.get(tool_name).spec, inst)


@router.get("/tool-calls", response_model=list[ToolCallOut])
async def list_tool_calls(
    session: SessionDep,
    ctx: TenantDep,
    execution_id: uuid.UUID | None = None,
    tool_name: Annotated[str | None, Query(max_length=96)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: datetime | None = None,
) -> list[ToolCallOut]:
    rows = await tool_service.list_tool_calls(
        session, ctx, execution_id=execution_id, tool_name=tool_name, limit=limit, before=before
    )
    return [ToolCallOut.model_validate(r) for r in rows]


@router.post("/demo-data")
async def seed_demo_data(
    request: Request, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> dict[str, Any]:
    result, installed = await tool_service.seed_demo(
        session, ctx, _executor(request).catalog, request=meta
    )
    return {
        "customers": result.customers,
        "orders": result.orders,
        "skipped": result.skipped,
        "tools_installed": installed,
    }


@router.get("/simulated/activity")
async def simulated_activity(session: SessionDep, ctx: TenantDep) -> dict[str, Any]:
    return await tool_service.simulated_activity(session, ctx)


class ToolPolicyOut(BaseModel):
    policy: ToolPolicyConfig
    updated_at: datetime | None


@router.get("/tool-policy", response_model=ToolPolicyOut)
async def get_tool_policy(session: SessionDep, ctx: TenantDep) -> ToolPolicyOut:
    policy, updated = await tool_service.get_tool_policy(session, ctx)
    return ToolPolicyOut(policy=policy, updated_at=updated)


@router.put("/tool-policy", response_model=ToolPolicyOut)
async def put_tool_policy(
    body: ToolPolicyConfig,
    request: Request,
    session: SessionDep,
    ctx: TenantDep,
    meta: RequestMetaDep,
) -> ToolPolicyOut:
    policy = await tool_service.set_tool_policy(
        session, ctx, _executor(request).catalog, body, request=meta
    )
    _, updated = await tool_service.get_tool_policy(session, ctx)
    return ToolPolicyOut(policy=policy, updated_at=updated)
