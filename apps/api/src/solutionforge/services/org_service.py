"""Organizations, memberships and invitations."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.core.config import Settings
from solutionforge.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from solutionforge.db.tenancy import TenantContext, scoped_select
from solutionforge.domain.audit import AuditEventType
from solutionforge.domain.identity import Invitation, Membership, Organization, User
from solutionforge.security import tokens
from solutionforge.security.rbac import Permission, Role, can_assign_role
from solutionforge.services import audit_service
from solutionforge.services.audit_service import RequestMeta
from solutionforge.services.auth_service import normalize_email
from solutionforge.services.authz import ensure

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50].strip("-")
    return slug if len(slug) >= 3 else f"org-{slug}".strip("-")


# --------------------------------------------------------------------------- orgs


async def create_organization(
    session: AsyncSession, *, user: User, name: str, slug: str | None, request: RequestMeta
) -> tuple[Organization, Membership]:
    explicit_slug = slug is not None
    candidate = slug or slugify(name)
    if not SLUG_RE.match(candidate):
        raise ValidationFailed("Invalid organization slug")
    if not explicit_slug and await _slug_taken(session, candidate):
        candidate = f"{candidate[:56]}-{secrets.token_hex(3)}"

    org = Organization(name=name, slug=candidate, created_by_user_id=user.id)
    session.add(org)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("Organization slug is already taken") from exc

    membership = Membership(organization_id=org.id, user_id=user.id, role=Role.OWNER)
    session.add(membership)
    audit_service.record(
        session,
        event_type=AuditEventType.ORG_CREATED,
        request=request,
        actor_user_id=user.id,
        organization_id=org.id,
        resource_type="organization",
        resource_id=org.id,
        metadata={"name": name, "slug": candidate},
    )
    audit_service.record(
        session,
        event_type=AuditEventType.MEMBER_ADDED,
        request=request,
        actor_user_id=user.id,
        organization_id=org.id,
        resource_type="user",
        resource_id=user.id,
        metadata={"role": Role.OWNER.value, "via": "org_creation"},
    )
    await session.commit()
    return org, membership


async def list_user_organizations(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[Organization, Role]]:
    rows = await session.execute(
        sa.select(Organization, Membership.role)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(Membership.user_id == user_id)
        .order_by(Organization.created_at)
    )
    return [(org, role) for org, role in rows]


async def resolve_tenant(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> TenantContext:
    """The only way to obtain a TenantContext: proves the user is a member of the org.

    Non-membership and non-existence both raise NotFound so org IDs cannot be probed.
    """
    role = await session.scalar(
        sa.select(Membership.role).where(
            Membership.organization_id == organization_id, Membership.user_id == user_id
        )
    )
    if role is None:
        raise NotFound("Organization not found")
    return TenantContext(organization_id=organization_id, user_id=user_id, role=role)


async def get_organization(session: AsyncSession, ctx: TenantContext) -> Organization:
    ensure(ctx, Permission.ORG_READ)
    org = await session.get(Organization, ctx.organization_id)
    if org is None:
        raise NotFound("Organization not found")
    return org


async def update_organization(
    session: AsyncSession, ctx: TenantContext, *, name: str, request: RequestMeta
) -> Organization:
    ensure(ctx, Permission.ORG_MANAGE)
    org = await get_organization(session, ctx)
    old = org.name
    org.name = name
    audit_service.record(
        session,
        event_type=AuditEventType.ORG_UPDATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="organization",
        resource_id=org.id,
        metadata={"name": {"from": old, "to": name}},
    )
    await session.commit()
    return org


# --------------------------------------------------------------------------- members


async def list_members(session: AsyncSession, ctx: TenantContext) -> list[Membership]:
    ensure(ctx, Permission.MEMBER_READ)
    stmt = scoped_select(Membership, ctx).order_by(Membership.created_at)
    return list((await session.scalars(stmt)).unique().all())


async def change_member_role(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    member_user_id: uuid.UUID,
    new_role: Role,
    request: RequestMeta,
) -> Membership:
    ensure(ctx, Permission.MEMBER_MANAGE)
    target = await _get_member(session, ctx, member_user_id)
    if not can_assign_role(ctx.role, target.role, new_role):
        raise PermissionDenied("You cannot assign this role to this member")
    if target.role == new_role:
        return target
    if target.role == Role.OWNER:
        await _ensure_not_last_owner(session, ctx)

    old = target.role
    target.role = new_role
    audit_service.record(
        session,
        event_type=AuditEventType.MEMBER_ROLE_CHANGED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="user",
        resource_id=member_user_id,
        metadata={"from": old.value, "to": new_role.value},
    )
    await session.commit()
    return target


async def remove_member(
    session: AsyncSession, ctx: TenantContext, *, member_user_id: uuid.UUID, request: RequestMeta
) -> None:
    leaving_self = member_user_id == ctx.user_id
    target = await _get_member(session, ctx, member_user_id)
    if not leaving_self:
        ensure(ctx, Permission.MEMBER_MANAGE)
        # Same rank rule as role changes: you cannot remove someone above you.
        if not can_assign_role(ctx.role, target.role, Role.VIEWER):
            raise PermissionDenied("You cannot remove this member")
    if target.role == Role.OWNER:
        await _ensure_not_last_owner(session, ctx)

    await session.delete(target)
    audit_service.record(
        session,
        event_type=AuditEventType.MEMBER_REMOVED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="user",
        resource_id=member_user_id,
        metadata={"role": target.role.value, "self": leaving_self},
    )
    await session.commit()


async def _get_member(session: AsyncSession, ctx: TenantContext, user_id: uuid.UUID) -> Membership:
    member = await session.scalar(scoped_select(Membership, ctx, Membership.user_id == user_id))
    if member is None:
        raise NotFound("Member not found")
    return member


def owners_for_update(ctx: TenantContext) -> sa.Select[Membership]:
    """Lock the org's owner rows. FOR UPDATE serializes concurrent demotions on PostgreSQL
    (two owners demoting each other at once); SQLite ignores it but serializes writers."""
    return scoped_select(Membership, ctx, Membership.role == Role.OWNER).with_for_update(
        of=Membership
    )


async def _ensure_not_last_owner(session: AsyncSession, ctx: TenantContext) -> None:
    owners = (await session.scalars(owners_for_update(ctx))).unique().all()
    if len(owners) <= 1:
        raise Conflict("An organization must keep at least one owner")


# --------------------------------------------------------------------------- invitations


@dataclass(frozen=True, slots=True)
class CreatedInvitation:
    invitation: Invitation
    token: str  # returned exactly once; only the hash is stored


async def create_invitation(
    session: AsyncSession,
    ctx: TenantContext,
    settings: Settings,
    *,
    email: str,
    role: Role,
    request: RequestMeta,
) -> CreatedInvitation:
    ensure(ctx, Permission.MEMBER_MANAGE)
    if not can_assign_role(ctx.role, None, role):
        raise PermissionDenied("You cannot invite members with this role")
    email = normalize_email(email)

    existing_member = await session.scalar(
        scoped_select(Membership, ctx)
        .join(User, User.id == Membership.user_id)
        .where(User.email == email)
    )
    if existing_member is not None:
        raise Conflict("This user is already a member")

    now = utcnow()
    raw = tokens.new_opaque_token()
    invitation = Invitation(
        organization_id=ctx.organization_id,
        email=email,
        role=role,
        token_hash=tokens.hash_opaque_token(raw),
        invited_by_user_id=ctx.user_id,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.invitation_ttl_seconds),
    )
    session.add(invitation)
    await session.flush()
    audit_service.record(
        session,
        event_type=AuditEventType.INVITATION_CREATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="invitation",
        resource_id=invitation.id,
        metadata={"email": email, "role": role.value},
    )
    await session.commit()
    return CreatedInvitation(invitation=invitation, token=raw)


async def list_pending_invitations(session: AsyncSession, ctx: TenantContext) -> list[Invitation]:
    ensure(ctx, Permission.MEMBER_MANAGE)
    stmt = scoped_select(
        Invitation,
        ctx,
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > utcnow(),
    ).order_by(Invitation.created_at.desc())
    return list((await session.scalars(stmt)).all())


async def revoke_invitation(
    session: AsyncSession, ctx: TenantContext, *, invitation_id: uuid.UUID, request: RequestMeta
) -> None:
    ensure(ctx, Permission.MEMBER_MANAGE)
    inv = await session.scalar(scoped_select(Invitation, ctx, Invitation.id == invitation_id))
    if inv is None or inv.accepted_at is not None:
        raise NotFound("Invitation not found")
    if inv.revoked_at is not None:
        return
    inv.revoked_at = utcnow()
    audit_service.record(
        session,
        event_type=AuditEventType.INVITATION_REVOKED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="invitation",
        resource_id=inv.id,
    )
    await session.commit()


async def accept_invitation(
    session: AsyncSession, *, user: User, token: str, request: RequestMeta
) -> tuple[Organization, Membership]:
    now = utcnow()
    inv = await session.scalar(
        sa.select(Invitation).where(Invitation.token_hash == tokens.hash_opaque_token(token))
    )
    # One generic error for every failure mode: no oracle for guessing tokens or emails.
    if (
        inv is None
        or inv.accepted_at is not None
        or inv.revoked_at is not None
        or inv.expires_at <= now
        or inv.email != user.email
    ):
        raise NotFound("Invitation not found or no longer valid")

    already = await session.scalar(
        sa.select(Membership.id).where(
            Membership.organization_id == inv.organization_id, Membership.user_id == user.id
        )
    )
    if already is not None:
        raise Conflict("You are already a member of this organization")

    membership = Membership(organization_id=inv.organization_id, user_id=user.id, role=inv.role)
    session.add(membership)
    inv.accepted_at = now
    inv.accepted_by_user_id = user.id
    audit_service.record(
        session,
        event_type=AuditEventType.INVITATION_ACCEPTED,
        request=request,
        actor_user_id=user.id,
        organization_id=inv.organization_id,
        resource_type="invitation",
        resource_id=inv.id,
        metadata={"role": inv.role.value},
    )
    audit_service.record(
        session,
        event_type=AuditEventType.MEMBER_ADDED,
        request=request,
        actor_user_id=user.id,
        organization_id=inv.organization_id,
        resource_type="user",
        resource_id=user.id,
        metadata={"role": inv.role.value, "via": "invitation"},
    )
    try:
        await session.flush()
    except IntegrityError as exc:  # concurrent accept of two invitations to the same org
        await session.rollback()
        raise Conflict("You are already a member of this organization") from exc
    org = await session.get(Organization, inv.organization_id)
    assert org is not None
    await session.commit()
    return org, membership


async def _slug_taken(session: AsyncSession, slug: str) -> bool:
    return (
        await session.scalar(sa.select(Organization.id).where(Organization.slug == slug))
        is not None
    )
