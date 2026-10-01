"""Tool installations, credentials, the tool-call trail, and demo data."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.connectors.seed import SeedResult, seed_simulated_systems
from solutionforge.core.clock import utcnow
from solutionforge.core.errors import ValidationFailed
from solutionforge.db.tenancy import TenantContext, scoped_select
from solutionforge.domain.audit import AuditEventType
from solutionforge.domain.policy import OrgPolicy
from solutionforge.domain.simulated import SimMessage, SimRefund, SimTicket
from solutionforge.domain.tools import ToolCall, ToolInstallation
from solutionforge.security.crypto import CredentialCipher
from solutionforge.security.rbac import Permission
from solutionforge.services import audit_service
from solutionforge.services.audit_service import RequestMeta
from solutionforge.services.authz import ensure
from solutionforge.tools.catalog import ToolCatalog
from solutionforge.tools.policy import ToolPolicyConfig
from solutionforge.tools.spec import ToolSpec

MAX_CREDENTIAL_BYTES = 4096
_UNSET: Any = object()


async def list_catalog(
    session: AsyncSession, ctx: TenantContext, catalog: ToolCatalog
) -> list[tuple[ToolSpec, ToolInstallation | None]]:
    ensure(ctx, Permission.TOOL_READ)
    installed = {
        i.tool_name: i for i in (await session.scalars(scoped_select(ToolInstallation, ctx))).all()
    }
    return [(spec, installed.get(spec.name)) for spec in catalog.specs()]


async def upsert_installation(
    session: AsyncSession,
    ctx: TenantContext,
    catalog: ToolCatalog,
    cipher: CredentialCipher,
    *,
    tool_name: str,
    enabled: bool,
    auto_approve_low_risk: bool,
    config: dict[str, str],
    credentials: dict[str, str] | Any | None = _UNSET,
    request: RequestMeta,
) -> ToolInstallation:
    """``credentials``: omitted → unchanged; ``None`` → cleared; dict → replaced."""
    ensure(ctx, Permission.TOOL_MANAGE)
    spec = catalog.get(tool_name).spec  # ToolNotFound → 404 via handler
    unknown = sorted(set(config) - set(spec.config_keys))
    if unknown:
        raise ValidationFailed(
            f"unknown config keys for {tool_name}: {unknown}",
            details={"allowed": list(spec.config_keys)},
        )

    inst = await session.scalar(
        scoped_select(
            ToolInstallation, ctx, ToolInstallation.tool_name == tool_name
        ).with_for_update()
    )
    if inst is None:
        inst = ToolInstallation(organization_id=ctx.organization_id, tool_name=tool_name)
        session.add(inst)
    inst.enabled = enabled
    inst.auto_approve_low_risk = auto_approve_low_risk
    inst.config = dict(config)
    inst.updated_by_user_id = ctx.user_id

    credential_action: str | None = None
    if credentials is not _UNSET:
        if credentials is None:
            inst.credentials_encrypted = None
            credential_action = "cleared"
        else:
            if len(json.dumps(credentials)) > MAX_CREDENTIAL_BYTES:
                raise ValidationFailed("credentials payload too large")
            inst.credentials_encrypted = cipher.encrypt(credentials)
            credential_action = "set"
    await session.flush()

    audit_service.record(
        session,
        event_type=AuditEventType.TOOL_INSTALLATION_UPDATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="tool",
        resource_id=tool_name,
        metadata={
            "enabled": enabled,
            "auto_approve_low_risk": auto_approve_low_risk,
            "config_keys": sorted(config),
            "risk_level": spec.risk_level.value,
        },
    )
    if credential_action is not None:
        audit_service.record(
            session,
            event_type=AuditEventType.CREDENTIALS_UPDATED,
            request=request,
            actor_user_id=ctx.user_id,
            organization_id=ctx.organization_id,
            resource_type="tool",
            resource_id=tool_name,
            # Field *names* only: values are never written anywhere in plaintext.
            metadata={"action": credential_action, "fields": sorted(credentials or {})},
        )
    await session.commit()
    return inst


async def list_tool_calls(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    execution_id: uuid.UUID | None,
    tool_name: str | None,
    limit: int,
    before: datetime | None,
) -> list[ToolCall]:
    ensure(ctx, Permission.TOOL_READ)
    stmt = scoped_select(ToolCall, ctx)
    if execution_id is not None:
        stmt = stmt.where(ToolCall.execution_id == execution_id)
    if tool_name is not None:
        stmt = stmt.where(ToolCall.tool_name == tool_name)
    if before is not None:
        stmt = stmt.where(ToolCall.created_at < before)
    stmt = stmt.order_by(ToolCall.created_at.desc(), ToolCall.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def seed_demo(
    session: AsyncSession,
    ctx: TenantContext,
    catalog: ToolCatalog,
    *,
    request: RequestMeta,
) -> tuple[SeedResult, list[str]]:
    """Populate the simulated systems and install every simulated tool (enabled, default
    policy). Idempotent. Credentialed tools are installed without credentials."""
    ensure(ctx, Permission.ORG_MANAGE)
    result = await seed_simulated_systems(session, ctx.organization_id, today=utcnow().date())
    existing = set(
        (
            await session.scalars(
                sa.select(ToolInstallation.tool_name).where(
                    ToolInstallation.organization_id == ctx.organization_id
                )
            )
        ).all()
    )
    installed = []
    for spec in catalog.specs():
        if spec.name not in existing:
            session.add(
                ToolInstallation(
                    organization_id=ctx.organization_id,
                    tool_name=spec.name,
                    enabled=True,
                    auto_approve_low_risk=True,
                    config={},
                    updated_by_user_id=ctx.user_id,
                )
            )
            installed.append(spec.name)
    audit_service.record(
        session,
        event_type=AuditEventType.DEMO_DATA_SEEDED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        metadata={
            "customers": result.customers,
            "orders": result.orders,
            "skipped": result.skipped,
            "tools_installed": installed,
        },
    )
    await session.commit()
    return result, installed


async def simulated_activity(
    session: AsyncSession, ctx: TenantContext
) -> dict[str, list[dict[str, Any]]]:
    """Side effects in the simulated systems, so humans can verify what tools did."""
    ensure(ctx, Permission.TOOL_READ)
    tickets = (
        await session.scalars(scoped_select(SimTicket, ctx).order_by(SimTicket.created_at))
    ).all()
    messages = (
        await session.scalars(scoped_select(SimMessage, ctx).order_by(SimMessage.created_at))
    ).all()
    refunds = (
        await session.scalars(scoped_select(SimRefund, ctx).order_by(SimRefund.created_at))
    ).all()
    return {
        "tickets": [
            {
                "ref": t.ref,
                "subject": t.subject,
                "priority": t.priority,
                "status": t.status,
                "created_at": t.created_at.isoformat(),
            }
            for t in tickets
        ],
        "messages": [
            {
                "id": str(m.id),
                "to": m.to_address,
                "subject": m.subject,
                "status": m.status,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
            }
            for m in messages
        ],
        "refunds": [
            {"id": str(r.id), "amount_cents": r.amount_cents, "reason": r.reason} for r in refunds
        ],
    }


async def get_tool_policy(
    session: AsyncSession, ctx: TenantContext
) -> tuple[ToolPolicyConfig, datetime | None]:
    ensure(ctx, Permission.TOOL_READ)
    row = await session.scalar(scoped_select(OrgPolicy, ctx))
    if row is None:
        return ToolPolicyConfig(), None
    return ToolPolicyConfig.model_validate(row.tool_policy), row.updated_at


async def set_tool_policy(
    session: AsyncSession,
    ctx: TenantContext,
    catalog: ToolCatalog,
    config: ToolPolicyConfig,
    *,
    request: RequestMeta,
) -> ToolPolicyConfig:
    ensure(ctx, Permission.ORG_MANAGE)
    unknown = sorted(t for t in config.blocked_tools if not catalog.has(t))
    if unknown:
        raise ValidationFailed(f"unknown tools in blocked_tools: {unknown}")
    row = await session.scalar(scoped_select(OrgPolicy, ctx).with_for_update())
    before = row.tool_policy if row is not None else None
    if row is None:
        row = OrgPolicy(organization_id=ctx.organization_id)
        session.add(row)
    row.tool_policy = config.model_dump(mode="json")
    row.updated_by_user_id = ctx.user_id
    row.updated_at = utcnow()
    await session.flush()
    audit_service.record(
        session,
        event_type=AuditEventType.POLICY_UPDATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="org_policy",
        resource_id=row.id,
        metadata={"before": before, "after": row.tool_policy},
    )
    await session.commit()
    return config
