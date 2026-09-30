"""LLM usage reporting and organization budgets."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from solutionforge.api.deps import RequestMetaDep, SessionDep, TenantDep
from solutionforge.domain.usage import OrgBudget
from solutionforge.llm.pricing import micro_to_usd, usd_to_micro
from solutionforge.services import usage_service

router = APIRouter(prefix="/orgs/{org_id}", tags=["usage"])

USD = Annotated[Decimal, Field(ge=0, le=1_000_000, decimal_places=6)]


class BudgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_limit_usd: USD | None = None
    monthly_limit_usd: USD | None = None
    per_execution_limit_usd: USD | None = None


class BudgetOut(BaseModel):
    daily_limit_usd: Decimal | None
    monthly_limit_usd: Decimal | None
    per_execution_limit_usd: Decimal | None
    updated_at: datetime | None


class UsageSummaryOut(BaseModel):
    model: str
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    avg_latency_ms: int


class UsageRecordOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    execution_id: uuid.UUID | None
    step_id: str | None
    model: str
    outcome: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal
    latency_ms: int
    attempt: int
    fallback_index: int


def _opt_usd(micro: int | None) -> Decimal | None:
    return None if micro is None else micro_to_usd(micro)


def _opt_micro(usd: Decimal | None) -> int | None:
    return None if usd is None else usd_to_micro(usd)


def _budget_out(b: OrgBudget | None) -> BudgetOut:
    if b is None:
        return BudgetOut(
            daily_limit_usd=None,
            monthly_limit_usd=None,
            per_execution_limit_usd=None,
            updated_at=None,
        )
    return BudgetOut(
        daily_limit_usd=_opt_usd(b.daily_limit_micro_usd),
        monthly_limit_usd=_opt_usd(b.monthly_limit_micro_usd),
        per_execution_limit_usd=_opt_usd(b.per_execution_limit_micro_usd),
        updated_at=b.updated_at,
    )


@router.get("/budget", response_model=BudgetOut)
async def get_budget(session: SessionDep, ctx: TenantDep) -> BudgetOut:
    return _budget_out(await usage_service.get_budget(session, ctx))


@router.put("/budget", response_model=BudgetOut)
async def put_budget(
    body: BudgetIn, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> BudgetOut:
    """Replace all limits. Omitted/null = unlimited."""
    b = await usage_service.set_budget(
        session,
        ctx,
        daily=_opt_micro(body.daily_limit_usd),
        monthly=_opt_micro(body.monthly_limit_usd),
        per_execution=_opt_micro(body.per_execution_limit_usd),
        request=meta,
    )
    return _budget_out(b)


@router.get("/usage/summary", response_model=list[UsageSummaryOut])
async def usage_summary(
    session: SessionDep,
    ctx: TenantDep,
    since: datetime | None = None,
    until: datetime | None = None,
    execution_id: uuid.UUID | None = None,
) -> list[UsageSummaryOut]:
    rows = await usage_service.summarize(
        session, ctx, since=since, until=until, execution_id=execution_id
    )
    return [
        UsageSummaryOut(
            model=r.model,
            calls=r.calls,
            failed_calls=r.failed_calls,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cost_usd=micro_to_usd(r.cost_micro_usd),
            avg_latency_ms=r.avg_latency_ms,
        )
        for r in rows
    ]


@router.get("/usage/records", response_model=list[UsageRecordOut])
async def usage_records(
    session: SessionDep,
    ctx: TenantDep,
    execution_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before: datetime | None = None,
) -> list[UsageRecordOut]:
    rows = await usage_service.list_records(
        session, ctx, execution_id=execution_id, limit=limit, before=before
    )
    return [
        UsageRecordOut(
            id=r.id,
            created_at=r.created_at,
            execution_id=r.execution_id,
            step_id=r.step_id,
            model=f"{r.provider}:{r.model}",
            outcome=r.outcome,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_read_tokens=r.cache_read_tokens,
            cache_write_tokens=r.cache_write_tokens,
            cost_usd=micro_to_usd(r.cost_micro_usd),
            latency_ms=r.latency_ms,
            attempt=r.attempt,
            fallback_index=r.fallback_index,
        )
        for r in rows
    ]
