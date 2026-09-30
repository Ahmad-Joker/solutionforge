"""Usage ledger (DB-backed), budgets, and usage reporting."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.clock import utcnow
from solutionforge.db.tenancy import TenantContext, scoped_select
from solutionforge.domain.audit import AuditEventType
from solutionforge.domain.usage import OrgBudget, UsageRecord
from solutionforge.llm.service import OrgLimits, Spend, UsageEntry
from solutionforge.llm.types import CallContext
from solutionforge.security.rbac import Permission
from solutionforge.services import audit_service
from solutionforge.services.audit_service import RequestMeta
from solutionforge.services.authz import ensure

_TOKENS = (
    UsageRecord.input_tokens
    + UsageRecord.output_tokens
    + UsageRecord.cache_read_tokens
    + UsageRecord.cache_write_tokens
)


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime) -> datetime:
    return day_start(now).replace(day=1)


class DbUsageLedger:
    """Implements :class:`solutionforge.llm.service.UsageLedger` on PostgreSQL.

    Each record is committed in its own short transaction so usage is persisted even if the
    surrounding workflow step later fails: the ledger is the billing source of truth.

    Budget checks are *pre-checks* against committed spend. Concurrent calls for the same
    org can each pass and overshoot by at most (concurrency x one call's worst case). This is
    documented and bounded; strict reservation is a later option (see BACKLOG).
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def record(self, entry: UsageEntry) -> None:
        async with self.sessionmaker() as s, s.begin():
            s.add(
                UsageRecord(
                    organization_id=entry.organization_id,
                    execution_id=entry.execution_id,
                    step_id=entry.step_id,
                    purpose=entry.purpose,
                    provider=entry.provider,
                    model=entry.model,
                    outcome=entry.outcome,
                    input_tokens=entry.usage.input_tokens,
                    output_tokens=entry.usage.output_tokens,
                    cache_read_tokens=entry.usage.cache_read_tokens,
                    cache_write_tokens=entry.usage.cache_write_tokens,
                    cost_micro_usd=entry.cost_micro_usd,
                    latency_ms=entry.latency_ms,
                    attempt=entry.attempt,
                    fallback_index=entry.fallback_index,
                    provider_request_id=entry.request_id,
                )
            )

    async def limits(self, organization_id: uuid.UUID) -> OrgLimits:
        async with self.sessionmaker() as s:
            b = await s.scalar(
                sa.select(OrgBudget).where(OrgBudget.organization_id == organization_id)
            )
        if b is None:
            return OrgLimits()
        return OrgLimits(
            b.daily_limit_micro_usd, b.monthly_limit_micro_usd, b.per_execution_limit_micro_usd
        )

    async def spent(self, ctx: CallContext) -> Spend:
        now = utcnow()
        org = UsageRecord.organization_id == ctx.organization_id
        cost = sa.func.coalesce(sa.func.sum(UsageRecord.cost_micro_usd), 0)
        async with self.sessionmaker() as s:
            day = await s.scalar(
                sa.select(cost).where(org, UsageRecord.created_at >= day_start(now))
            )
            month = await s.scalar(
                sa.select(cost).where(org, UsageRecord.created_at >= month_start(now))
            )
            exec_cost, exec_tokens = 0, 0
            if ctx.execution_id is not None:
                row = (
                    await s.execute(
                        sa.select(cost, sa.func.coalesce(sa.func.sum(_TOKENS), 0)).where(
                            org, UsageRecord.execution_id == ctx.execution_id
                        )
                    )
                ).one()
                exec_cost, exec_tokens = int(row[0]), int(row[1])
        return Spend(int(day or 0), int(month or 0), exec_cost, exec_tokens)


# --------------------------------------------------------------------------- budgets API


async def get_budget(session: AsyncSession, ctx: TenantContext) -> OrgBudget | None:
    ensure(ctx, Permission.USAGE_READ)
    return await session.scalar(scoped_select(OrgBudget, ctx))


async def set_budget(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    daily: int | None,
    monthly: int | None,
    per_execution: int | None,
    request: RequestMeta,
) -> OrgBudget:
    ensure(ctx, Permission.ORG_MANAGE)
    budget = await session.scalar(scoped_select(OrgBudget, ctx).with_for_update())
    before = (
        None
        if budget is None
        else [
            budget.daily_limit_micro_usd,
            budget.monthly_limit_micro_usd,
            budget.per_execution_limit_micro_usd,
        ]
    )
    if budget is None:
        budget = OrgBudget(organization_id=ctx.organization_id)
        session.add(budget)
    budget.daily_limit_micro_usd = daily
    budget.monthly_limit_micro_usd = monthly
    budget.per_execution_limit_micro_usd = per_execution
    budget.updated_by_user_id = ctx.user_id
    budget.updated_at = utcnow()
    await session.flush()
    audit_service.record(
        session,
        event_type=AuditEventType.BUDGET_UPDATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="org_budget",
        resource_id=budget.id,
        metadata={"before_micro_usd": before, "after_micro_usd": [daily, monthly, per_execution]},
    )
    await session.commit()
    return budget


# --------------------------------------------------------------------------- reporting


@dataclass(frozen=True, slots=True)
class UsageSummaryRow:
    model: str
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    avg_latency_ms: int


async def summarize(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    since: datetime | None,
    until: datetime | None,
    execution_id: uuid.UUID | None = None,
) -> list[UsageSummaryRow]:
    ensure(ctx, Permission.USAGE_READ)
    since = since or (utcnow() - timedelta(days=30))
    conditions = [
        UsageRecord.organization_id == ctx.organization_id,
        UsageRecord.created_at >= since,
    ]
    if until is not None:
        conditions.append(UsageRecord.created_at < until)
    if execution_id is not None:
        conditions.append(UsageRecord.execution_id == execution_id)
    model = UsageRecord.provider + ":" + UsageRecord.model
    rows = await session.execute(
        sa.select(
            model,
            sa.func.count(),
            sa.func.sum(sa.case((UsageRecord.outcome != "ok", 1), else_=0)),
            sa.func.sum(UsageRecord.input_tokens),
            sa.func.sum(UsageRecord.output_tokens),
            sa.func.sum(UsageRecord.cost_micro_usd),
            sa.func.avg(UsageRecord.latency_ms),
        )
        .where(*conditions)
        .group_by(model)
        .order_by(model)
    )
    return [
        UsageSummaryRow(
            m, int(c), int(f or 0), int(i or 0), int(o or 0), int(cost or 0), int(lat or 0)
        )
        for m, c, f, i, o, cost, lat in rows
    ]


async def list_records(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    execution_id: uuid.UUID | None,
    limit: int,
    before: datetime | None,
) -> list[UsageRecord]:
    ensure(ctx, Permission.USAGE_READ)
    stmt = scoped_select(UsageRecord, ctx)
    if execution_id is not None:
        stmt = stmt.where(UsageRecord.execution_id == execution_id)
    if before is not None:
        stmt = stmt.where(UsageRecord.created_at < before)
    stmt = stmt.order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def execution_totals(
    session: AsyncSession, organization_id: uuid.UUID, execution_id: uuid.UUID
) -> tuple[int, int, int]:
    """(calls, tokens, cost_micro_usd) for one execution; caller has already authorized."""
    row = (
        await session.execute(
            sa.select(
                sa.func.count(),
                sa.func.coalesce(sa.func.sum(_TOKENS), 0),
                sa.func.coalesce(sa.func.sum(UsageRecord.cost_micro_usd), 0),
            ).where(
                UsageRecord.organization_id == organization_id,
                UsageRecord.execution_id == execution_id,
            )
        )
    ).one()
    return int(row[0]), int(row[1]), int(row[2])
