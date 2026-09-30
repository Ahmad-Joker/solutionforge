"""Cross-tenant access must be impossible, and indistinguishable from 'does not exist'.

Setup: two orgs, each with an owner. Each test has tenant A's owner (highest privilege in
their own org) attack tenant B's resources through every route shape we expose.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient

from tests.helpers import Api, Session

pytestmark = pytest.mark.security


@dataclass
class TwoTenants:
    a_owner: Session
    a_org: str
    b_owner: Session
    b_org: str
    b_member: Session
    b_invitation_id: str


@pytest.fixture
async def tenants(api: Api) -> TwoTenants:
    a_owner, b_owner = await api.user(), await api.user()
    a_org = await api.org(a_owner, "Tenant A")
    b_org = await api.org(b_owner, "Tenant B")
    b_member = await api.member(b_org, b_owner, "operator")
    inv = await api.invite(b_org, b_owner, "pending@example.com", "viewer")
    return TwoTenants(a_owner, a_org, b_owner, b_org, b_member, inv.json()["id"])


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/orgs/{b_org}", None),
        ("PATCH", "/api/v1/orgs/{b_org}", {"name": "pwned"}),
        ("GET", "/api/v1/orgs/{b_org}/members", None),
        ("PATCH", "/api/v1/orgs/{b_org}/members/{b_member}", {"role": "viewer"}),
        ("DELETE", "/api/v1/orgs/{b_org}/members/{b_member}", None),
        ("GET", "/api/v1/orgs/{b_org}/invitations", None),
        ("POST", "/api/v1/orgs/{b_org}/invitations", {"email": "x@example.com", "role": "owner"}),
        ("DELETE", "/api/v1/orgs/{b_org}/invitations/{b_inv}", None),
        ("GET", "/api/v1/orgs/{b_org}/audit-events", None),
    ],
)
async def test_owner_of_a_cannot_touch_b(
    client: AsyncClient, tenants: TwoTenants, method: str, path: str, body: dict[str, str] | None
) -> None:
    url = path.format(
        b_org=tenants.b_org, b_member=tenants.b_member.user_id, b_inv=tenants.b_invitation_id
    )
    r = await client.request(method, url, json=body, headers=tenants.a_owner.headers)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["message"] == "Organization not found"


async def test_foreign_org_and_nonexistent_org_look_identical(
    client: AsyncClient, tenants: TwoTenants
) -> None:
    foreign = await client.get(f"/api/v1/orgs/{tenants.b_org}", headers=tenants.a_owner.headers)
    missing = await client.get(f"/api/v1/orgs/{uuid.uuid4()}", headers=tenants.a_owner.headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


async def test_own_org_path_cannot_reach_foreign_member(
    client: AsyncClient, tenants: TwoTenants
) -> None:
    """Confused-deputy attempt: use my org in the path, the victim's user id in the resource."""
    t = tenants
    url = f"/api/v1/orgs/{t.a_org}/members/{t.b_member.user_id}"
    assert (
        await client.patch(url, json={"role": "viewer"}, headers=t.a_owner.headers)
    ).status_code == 404
    assert (await client.delete(url, headers=t.a_owner.headers)).status_code == 404
    # The victim's membership is untouched.
    members = (
        await client.get(f"/api/v1/orgs/{t.b_org}/members", headers=t.b_owner.headers)
    ).json()
    assert {m["user_id"]: m["role"] for m in members}[t.b_member.user_id] == "operator"


async def test_own_org_path_cannot_revoke_foreign_invitation(
    client: AsyncClient, tenants: TwoTenants
) -> None:
    t = tenants
    r = await client.delete(
        f"/api/v1/orgs/{t.a_org}/invitations/{t.b_invitation_id}", headers=t.a_owner.headers
    )
    assert r.status_code == 404
    pending = (
        await client.get(f"/api/v1/orgs/{t.b_org}/invitations", headers=t.b_owner.headers)
    ).json()
    assert [i["id"] for i in pending] == [t.b_invitation_id]


async def test_org_listing_only_shows_own_memberships(
    client: AsyncClient, tenants: TwoTenants
) -> None:
    orgs = (await client.get("/api/v1/orgs", headers=tenants.a_owner.headers)).json()
    assert [o["id"] for o in orgs] == [tenants.a_org]


async def test_audit_logs_do_not_leak_across_tenants(
    client: AsyncClient, tenants: TwoTenants
) -> None:
    a = (
        await client.get(
            f"/api/v1/orgs/{tenants.a_org}/audit-events", headers=tenants.a_owner.headers
        )
    ).json()
    b = (
        await client.get(
            f"/api/v1/orgs/{tenants.b_org}/audit-events", headers=tenants.b_owner.headers
        )
    ).json()
    assert a and b
    assert not {e["id"] for e in a} & {e["id"] for e in b}
    # Nothing about tenant B (its invitations, its member) appears in A's log.
    assert tenants.b_member.user_id not in str(a) and "pending@example.com" not in str(a)


async def test_removed_member_loses_access_immediately(
    api: Api, client: AsyncClient, tenants: TwoTenants
) -> None:
    """Roles are resolved per request, not baked into the JWT."""
    t = tenants
    assert (
        await client.get(f"/api/v1/orgs/{t.b_org}", headers=t.b_member.headers)
    ).status_code == 200
    await client.delete(
        f"/api/v1/orgs/{t.b_org}/members/{t.b_member.user_id}", headers=t.b_owner.headers
    )
    # Same, still-valid access token:
    assert (
        await client.get(f"/api/v1/orgs/{t.b_org}", headers=t.b_member.headers)
    ).status_code == 404
