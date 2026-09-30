"""The RBAC matrix, enforced end-to-end over HTTP for every role."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.helpers import Api, Session

pytestmark = pytest.mark.security

ROLES = ["viewer", "operator", "admin", "owner"]

# (method, path, body, minimum role that must succeed)
CASES = [
    ("GET", "/api/v1/orgs/{org}", None, "viewer"),
    ("GET", "/api/v1/orgs/{org}/members", None, "viewer"),
    ("PATCH", "/api/v1/orgs/{org}", {"name": "Renamed"}, "admin"),
    ("GET", "/api/v1/orgs/{org}/invitations", None, "admin"),
    (
        "POST",
        "/api/v1/orgs/{org}/invitations",
        {"email": "new@example.com", "role": "viewer"},
        "admin",
    ),
    ("GET", "/api/v1/orgs/{org}/audit-events", None, "admin"),
]


async def _actor(api: Api, role: str) -> tuple[str, Session]:
    owner = await api.user()
    org = await api.org(owner)
    return org, owner if role == "owner" else await api.member(org, owner, role)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(("method", "path", "body", "min_role"), CASES)
async def test_role_matrix(
    api: Api,
    client: AsyncClient,
    role: str,
    method: str,
    path: str,
    body: dict[str, str] | None,
    min_role: str,
) -> None:
    org, actor = await _actor(api, role)
    r = await client.request(method, path.format(org=org), json=body, headers=actor.headers)
    allowed = ROLES.index(role) >= ROLES.index(min_role)
    if allowed:
        assert r.status_code in (200, 201), r.text
    else:
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "permission_denied"


async def test_admin_cannot_invite_an_owner(api: Api) -> None:
    org, admin = await _actor(api, "admin")
    r = await api.invite(org, admin, "x@example.com", "owner")
    assert r.status_code == 403


async def test_admin_cannot_self_promote_to_owner(api: Api, client: AsyncClient) -> None:
    org, admin = await _actor(api, "admin")
    r = await client.patch(
        f"/api/v1/orgs/{org}/members/{admin.user_id}", json={"role": "owner"}, headers=admin.headers
    )
    assert r.status_code == 403


async def test_role_value_outside_enum_rejected(api: Api, client: AsyncClient) -> None:
    org, owner = await _actor(api, "owner")
    r = await client.patch(
        f"/api/v1/orgs/{org}/members/{owner.user_id}",
        json={"role": "superuser"},
        headers=owner.headers,
    )
    assert r.status_code == 422
