"""Organization, membership and invitation endpoints.

Every ``/orgs/{org_id}/...`` route depends on ``TenantDep``, which verifies membership
before any handler code runs. Handlers pass the resulting TenantContext to services,
which enforce permissions.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from solutionforge.api.deps import (
    CurrentUserDep,
    RequestMetaDep,
    SessionDep,
    SettingsDep,
    TenantDep,
)
from solutionforge.domain.identity import Membership
from solutionforge.schemas.identity import (
    AcceptInvitationRequest,
    ChangeRoleRequest,
    CreatedInvitationOut,
    CreateInvitationRequest,
    CreateOrganizationRequest,
    InvitationOut,
    MemberOut,
    MyOrganizationOut,
    OrganizationOut,
    UpdateOrganizationRequest,
)
from solutionforge.services import org_service

router = APIRouter(tags=["organizations"])


def _member_out(m: Membership) -> MemberOut:
    return MemberOut(
        user_id=m.user_id,
        email=m.user.email,
        display_name=m.user.display_name,
        role=m.role,
        joined_at=m.created_at,
    )


@router.post("/orgs", response_model=MyOrganizationOut, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: CreateOrganizationRequest, session: SessionDep, user: CurrentUserDep, meta: RequestMetaDep
) -> MyOrganizationOut:
    org, membership = await org_service.create_organization(
        session, user=user, name=body.name, slug=body.slug, request=meta
    )
    return MyOrganizationOut(
        id=org.id, name=org.name, slug=org.slug, created_at=org.created_at, role=membership.role
    )


@router.get("/orgs", response_model=list[MyOrganizationOut])
async def list_my_orgs(session: SessionDep, user: CurrentUserDep) -> list[MyOrganizationOut]:
    rows = await org_service.list_user_organizations(session, user.id)
    return [
        MyOrganizationOut(id=o.id, name=o.name, slug=o.slug, created_at=o.created_at, role=r)
        for o, r in rows
    ]


@router.get("/orgs/{org_id}", response_model=OrganizationOut)
async def get_org(session: SessionDep, ctx: TenantDep) -> OrganizationOut:
    return OrganizationOut.model_validate(await org_service.get_organization(session, ctx))


@router.patch("/orgs/{org_id}", response_model=OrganizationOut)
async def update_org(
    body: UpdateOrganizationRequest, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> OrganizationOut:
    org = await org_service.update_organization(session, ctx, name=body.name, request=meta)
    return OrganizationOut.model_validate(org)


@router.get("/orgs/{org_id}/members", response_model=list[MemberOut])
async def list_members(session: SessionDep, ctx: TenantDep) -> list[MemberOut]:
    return [_member_out(m) for m in await org_service.list_members(session, ctx)]


@router.patch("/orgs/{org_id}/members/{user_id}", response_model=MemberOut)
async def change_role(
    user_id: uuid.UUID,
    body: ChangeRoleRequest,
    session: SessionDep,
    ctx: TenantDep,
    meta: RequestMetaDep,
) -> MemberOut:
    m = await org_service.change_member_role(
        session, ctx, member_user_id=user_id, new_role=body.role, request=meta
    )
    return _member_out(m)


@router.delete("/orgs/{org_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> None:
    await org_service.remove_member(session, ctx, member_user_id=user_id, request=meta)


@router.post(
    "/orgs/{org_id}/invitations",
    response_model=CreatedInvitationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    body: CreateInvitationRequest,
    session: SessionDep,
    settings: SettingsDep,
    ctx: TenantDep,
    meta: RequestMetaDep,
) -> CreatedInvitationOut:
    created = await org_service.create_invitation(
        session, ctx, settings, email=body.email, role=body.role, request=meta
    )
    inv = created.invitation
    return CreatedInvitationOut(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        created_at=inv.created_at,
        expires_at=inv.expires_at,
        token=created.token,
    )


@router.get("/orgs/{org_id}/invitations", response_model=list[InvitationOut])
async def list_invitations(session: SessionDep, ctx: TenantDep) -> list[InvitationOut]:
    invs = await org_service.list_pending_invitations(session, ctx)
    return [InvitationOut.model_validate(i) for i in invs]


@router.delete("/orgs/{org_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: uuid.UUID, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> None:
    await org_service.revoke_invitation(session, ctx, invitation_id=invitation_id, request=meta)


@router.post("/invitations/accept", response_model=MyOrganizationOut)
async def accept_invitation(
    body: AcceptInvitationRequest, session: SessionDep, user: CurrentUserDep, meta: RequestMetaDep
) -> MyOrganizationOut:
    org, membership = await org_service.accept_invitation(
        session, user=user, token=body.token, request=meta
    )
    return MyOrganizationOut(
        id=org.id, name=org.name, slug=org.slug, created_at=org.created_at, role=membership.role
    )
