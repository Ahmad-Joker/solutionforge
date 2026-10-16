"""Business assistant: runs (ask a question), their tool activity, and ticket-draft approval."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from solutionforge.api.deps import RequestMetaDep, SessionDep, TenantDep
from solutionforge.assistant.service import AssistantConfig, AssistantService
from solutionforge.core.errors import NotFound
from solutionforge.db.tenancy import scoped_select
from solutionforge.domain.assistant import AssistantEvent, AssistantRun, DraftStatus, TicketDraft
from solutionforge.llm.pricing import micro_to_usd
from solutionforge.security.rbac import Permission
from solutionforge.services.authz import ensure

router = APIRouter(prefix="/orgs/{org_id}/assistant", tags=["assistant"])


def _svc(request: Request) -> AssistantService:
    svc: AssistantService = request.app.state.assistant
    return svc


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    knowledge_base_id: uuid.UUID
    model: str | None = Field(default=None, max_length=128, description="provider:model")
    tool_mode: Literal["model", "fixed"] = "model"


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    question: str
    answer: str | None
    citations: list[dict[str, Any]]
    provider: str
    model: str
    prompt_version: str
    corpus_version: str | None
    config: dict[str, Any]
    turns: int
    tool_calls: int
    retries: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    cost_usd: Decimal | None = None  # estimate from list prices; null = unknown
    latency_ms: int
    retrieval_ms: int
    tool_ms: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    drafts: list[DraftOut] = []


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: str
    name: str
    outcome: str
    detail: dict[str, Any]
    latency_ms: int
    created_at: datetime


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID | None
    subject: str
    description: str
    content_hash: str
    status: str
    ticket_ref: str | None
    decided_by_user_id: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


class DraftEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(max_length=200)
    description: str = Field(max_length=5000)


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(
        min_length=64, max_length=64, description="Hash of the draft version you reviewed"
    )


class ApproveOut(BaseModel):
    draft: DraftOut
    ticket_created: bool


class AssistantInfo(BaseModel):
    models: list[str]
    default_model: str
    prompt_version: str
    tools: list[dict[str, Any]]
    limits: dict[str, Any]


async def _run_out(session: Any, run: AssistantRun) -> RunOut:
    out = RunOut.model_validate(run)
    out.cost_usd = micro_to_usd(run.cost_micro_usd) if run.cost_micro_usd is not None else None
    drafts = (
        await session.scalars(
            sa.select(TicketDraft)
            .where(TicketDraft.run_id == run.id, TicketDraft.organization_id == run.organization_id)
            .order_by(TicketDraft.created_at)
        )
    ).all()
    out.drafts = [DraftOut.model_validate(d) for d in drafts]
    return out


@router.get("", response_model=AssistantInfo)
async def info(request: Request, ctx: TenantDep) -> AssistantInfo:
    from solutionforge.assistant.service import PROMPT_VERSION
    from solutionforge.assistant.tools import TOOL_SPECS

    ensure(ctx, Permission.WORKFLOW_READ)
    svc = _svc(request)
    cfg = AssistantConfig.from_settings(request.app.state.settings)
    models = svc.available_models(cfg.model)
    return AssistantInfo(
        models=models,
        default_model=models[0],
        prompt_version=PROMPT_VERSION,
        tools=[{"name": t.name, "description": t.description} for t in TOOL_SPECS],
        limits={
            "max_turns": cfg.max_turns,
            "max_tool_calls": cfg.max_tool_calls,
            "deadline_seconds": cfg.deadline_seconds,
            "max_retries": cfg.max_retries,
            "run_budget_usd": str(micro_to_usd(cfg.run_budget_micro_usd)),
        },
    )


@router.post("/runs", response_model=RunOut, status_code=201)
async def create_run(
    body: RunIn, request: Request, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> RunOut:
    svc = _svc(request)
    base = AssistantConfig.from_settings(request.app.state.settings)
    model = body.model or svc.available_models(base.model)[0]
    run = await svc.run(
        ctx,
        question=body.question,
        knowledge_base_id=body.knowledge_base_id,
        config=AssistantConfig.from_settings(
            request.app.state.settings, model=model, tool_mode=body.tool_mode
        ),
        meta=meta,
    )
    return await _run_out(session, run)


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    session: SessionDep, ctx: TenantDep, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[RunOut]:
    ensure(ctx, Permission.WORKFLOW_READ)
    runs = (
        await session.scalars(
            scoped_select(AssistantRun, ctx).order_by(AssistantRun.created_at.desc()).limit(limit)
        )
    ).all()
    return [await _run_out(session, r) for r in runs]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> RunOut:
    ensure(ctx, Permission.WORKFLOW_READ)
    run = await session.scalar(scoped_select(AssistantRun, ctx, AssistantRun.id == run_id))
    if run is None:
        raise NotFound("run not found")
    return await _run_out(session, run)


@router.get("/runs/{run_id}/events", response_model=list[EventOut])
async def run_events(
    run_id: uuid.UUID, session: SessionDep, ctx: TenantDep
) -> list[AssistantEvent]:
    ensure(ctx, Permission.WORKFLOW_READ)
    run = await session.scalar(scoped_select(AssistantRun, ctx, AssistantRun.id == run_id))
    if run is None:
        raise NotFound("run not found")
    return list(
        (
            await session.scalars(
                scoped_select(AssistantEvent, ctx, AssistantEvent.run_id == run_id).order_by(
                    AssistantEvent.seq
                )
            )
        ).all()
    )


@router.post("/runs/{run_id}/cancel", status_code=204)
async def cancel_run(run_id: uuid.UUID, request: Request, ctx: TenantDep) -> None:
    await _svc(request).cancel(ctx, run_id)


@router.get("/drafts", response_model=list[DraftOut])
async def list_drafts(
    session: SessionDep,
    ctx: TenantDep,
    status: DraftStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[TicketDraft]:
    ensure(ctx, Permission.APPROVAL_READ)
    stmt = scoped_select(TicketDraft, ctx)
    if status is not None:
        stmt = stmt.where(TicketDraft.status == status)
    return list(
        (await session.scalars(stmt.order_by(TicketDraft.created_at.desc()).limit(limit))).all()
    )


@router.patch("/drafts/{draft_id}", response_model=DraftOut)
async def edit_draft(
    draft_id: uuid.UUID, body: DraftEditIn, request: Request, ctx: TenantDep, meta: RequestMetaDep
) -> TicketDraft:
    return await _svc(request).edit_draft(ctx, draft_id, body.subject, body.description, meta)


@router.post("/drafts/{draft_id}/approve", response_model=ApproveOut)
async def approve_draft(
    draft_id: uuid.UUID, body: ApproveIn, request: Request, ctx: TenantDep, meta: RequestMetaDep
) -> ApproveOut:
    draft, created = await _svc(request).approve_draft(ctx, draft_id, body.content_hash, meta)
    return ApproveOut(draft=DraftOut.model_validate(draft), ticket_created=created)


@router.post("/drafts/{draft_id}/reject", response_model=DraftOut)
async def reject_draft(
    draft_id: uuid.UUID, request: Request, ctx: TenantDep, meta: RequestMetaDep
) -> TicketDraft:
    return await _svc(request).reject_draft(ctx, draft_id, meta)
