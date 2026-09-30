from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.domain import AuditEvent, Invitation
from tests.helpers import Api


async def test_create_org_makes_creator_owner(api: Api, client: AsyncClient) -> None:
    owner = await api.user()
    r = await client.post("/api/v1/orgs", json={"name": "Acme Corp"}, headers=owner.headers)
    assert r.status_code == 201
    body = r.json()
    assert body["slug"] == "acme-corp" and body["role"] == "owner"

    mine = (await client.get("/api/v1/orgs", headers=owner.headers)).json()
    assert [o["id"] for o in mine] == [body["id"]]


async def test_slug_collisions(api: Api, client: AsyncClient) -> None:
    a, b = await api.user(), await api.user()
    await api.org(a, "Acme Corp")
    # Derived slug collides -> suffixed automatically.
    r = await client.post("/api/v1/orgs", json={"name": "Acme Corp"}, headers=b.headers)
    assert r.status_code == 201 and r.json()["slug"].startswith("acme-corp-")
    # Explicit slug collides -> 409, never silently changed.
    r = await client.post(
        "/api/v1/orgs", json={"name": "Other", "slug": "acme-corp"}, headers=b.headers
    )
    assert r.status_code == 409


async def test_invalid_slug_rejected(api: Api, client: AsyncClient) -> None:
    u = await api.user()
    r = await client.post(
        "/api/v1/orgs", json={"name": "Other", "slug": "Bad Slug!"}, headers=u.headers
    )
    assert r.status_code == 422


async def test_invitation_flow(api: Api, client: AsyncClient) -> None:
    owner = await api.user()
    org = await api.org(owner)
    invitee = await api.user()

    r = await api.invite(org, owner, invitee.email.upper(), "operator")
    assert r.status_code == 201
    token = r.json()["token"]
    listed = (await client.get(f"/api/v1/orgs/{org}/invitations", headers=owner.headers)).json()
    assert len(listed) == 1 and "token" not in listed[0]

    r = await api.accept(invitee, token)
    assert r.status_code == 200 and r.json()["role"] == "operator"

    members = (await client.get(f"/api/v1/orgs/{org}/members", headers=owner.headers)).json()
    assert {m["role"] for m in members} == {"owner", "operator"}
    # Single use.
    assert (await api.accept(invitee, token)).status_code == 404
    assert (await client.get(f"/api/v1/orgs/{org}/invitations", headers=owner.headers)).json() == []


async def test_invitation_bound_to_invited_email(api: Api) -> None:
    owner = await api.user()
    org = await api.org(owner)
    intended, interloper = await api.user(), await api.user()
    token = (await api.invite(org, owner, intended.email, "viewer")).json()["token"]
    r = await api.accept(interloper, token)
    assert r.status_code == 404  # same response as a bad token
    assert (await api.accept(intended, token)).status_code == 200


async def test_expired_and_revoked_invitations(
    api: Api, client: AsyncClient, db: AsyncSession
) -> None:
    owner = await api.user()
    org = await api.org(owner)
    a, b = await api.user(), await api.user()

    r = await api.invite(org, owner, a.email, "viewer")
    await db.execute(
        sa.update(Invitation)
        .where(Invitation.email == a.email)
        .values(expires_at=utcnow() - timedelta(seconds=1))
    )
    await db.commit()
    assert (await api.accept(a, r.json()["token"])).status_code == 404

    r = await api.invite(org, owner, b.email, "viewer")
    inv_id = r.json()["id"]
    d = await client.delete(f"/api/v1/orgs/{org}/invitations/{inv_id}", headers=owner.headers)
    assert d.status_code == 204
    assert (await api.accept(b, r.json()["token"])).status_code == 404


async def test_cannot_invite_existing_member(api: Api) -> None:
    owner = await api.user()
    org = await api.org(owner)
    assert (await api.invite(org, owner, owner.email, "viewer")).status_code == 409


async def test_role_change_rules(api: Api, client: AsyncClient) -> None:
    owner = await api.user()
    org = await api.org(owner)
    admin = await api.member(org, owner, "admin")
    viewer = await api.member(org, owner, "viewer")

    async def set_role(actor_headers: dict[str, str], user_id: str, role: str) -> int:
        r = await client.patch(
            f"/api/v1/orgs/{org}/members/{user_id}", json={"role": role}, headers=actor_headers
        )
        return r.status_code

    assert await set_role(admin.headers, viewer.user_id, "operator") == 200
    assert await set_role(admin.headers, viewer.user_id, "owner") == 403  # above own rank
    assert await set_role(admin.headers, owner.user_id, "viewer") == 403  # higher-ranked target
    assert await set_role(viewer.headers, admin.user_id, "viewer") == 403  # no member:manage
    assert await set_role(owner.headers, admin.user_id, "owner") == 200
    # Now two owners: one may step down.
    assert await set_role(owner.headers, owner.user_id, "admin") == 200


async def test_last_owner_is_protected(api: Api, client: AsyncClient) -> None:
    owner = await api.user()
    org = await api.org(owner)
    r = await client.patch(
        f"/api/v1/orgs/{org}/members/{owner.user_id}", json={"role": "admin"}, headers=owner.headers
    )
    assert r.status_code == 409
    r = await client.delete(f"/api/v1/orgs/{org}/members/{owner.user_id}", headers=owner.headers)
    assert r.status_code == 409


async def test_member_can_leave_and_admin_can_remove(api: Api, client: AsyncClient) -> None:
    owner = await api.user()
    org = await api.org(owner)
    viewer = await api.member(org, owner, "viewer")
    admin = await api.member(org, owner, "admin")
    other = await api.member(org, owner, "operator")

    # Viewer cannot remove someone else, but can leave.
    assert (
        await client.delete(f"/api/v1/orgs/{org}/members/{other.user_id}", headers=viewer.headers)
    ).status_code == 403
    assert (
        await client.delete(f"/api/v1/orgs/{org}/members/{viewer.user_id}", headers=viewer.headers)
    ).status_code == 204
    assert (await client.get(f"/api/v1/orgs/{org}", headers=viewer.headers)).status_code == 404

    # Admin removes operator, but not the owner.
    assert (
        await client.delete(f"/api/v1/orgs/{org}/members/{other.user_id}", headers=admin.headers)
    ).status_code == 204
    assert (
        await client.delete(f"/api/v1/orgs/{org}/members/{owner.user_id}", headers=admin.headers)
    ).status_code == 403


async def test_update_org_and_audit_trail(api: Api, client: AsyncClient, db: AsyncSession) -> None:
    owner = await api.user()
    org = await api.org(owner)
    r = await client.patch(f"/api/v1/orgs/{org}", json={"name": "Renamed"}, headers=owner.headers)
    assert r.status_code == 200 and r.json()["name"] == "Renamed"

    events = (await client.get(f"/api/v1/orgs/{org}/audit-events", headers=owner.headers)).json()
    types = [e["event_type"] for e in events]
    assert types[0] == "org.updated"  # newest first
    assert {"org.created", "member.added"} <= set(types)
    assert events[0]["metadata"] == {"name": {"from": "Acme Corp", "to": "Renamed"}}
    assert all(e["request_id"] for e in events)

    filtered = (
        await client.get(
            f"/api/v1/orgs/{org}/audit-events",
            params={"event_type": "org.created"},
            headers=owner.headers,
        )
    ).json()
    assert [e["event_type"] for e in filtered] == ["org.created"]

    # Login events are user-scoped and must not appear in the tenant's log.
    assert await db.scalar(
        sa.select(AuditEvent.id).where(AuditEvent.event_type == "auth.login_succeeded")
    )
    assert "auth.login_succeeded" not in types


async def test_invitation_token_never_stored_in_plaintext(api: Api, db: AsyncSession) -> None:
    owner = await api.user()
    org = await api.org(owner)
    token = (await api.invite(org, owner, "z@example.com", "viewer")).json()["token"]
    inv = (await db.scalars(sa.select(Invitation))).one()
    assert inv.token_hash != token and token not in inv.token_hash
    audit = (await db.scalars(sa.select(AuditEvent.event_metadata))).all()
    assert token not in str(audit)
