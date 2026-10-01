"""Tools: tenant isolation of data reached through tools, installations, call trails; RBAC."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from solutionforge.tools.executor import ToolExecutor
from solutionforge.tools.spec import ToolBusinessError, ToolNotEnabled
from tests.helpers import Api

pytestmark = pytest.mark.security


async def _seeded(api: Api, client: AsyncClient) -> tuple[object, str]:
    owner = await api.user()
    org = await api.org(owner)
    assert (
        await client.post(f"/api/v1/orgs/{org}/demo-data", headers=owner.headers)
    ).status_code == 200
    return owner, org


async def test_tools_cannot_reach_another_tenants_data(
    api: Api, client: AsyncClient, tool_harness: ToolExecutor
) -> None:
    _, victim_org = await _seeded(api, client)
    # Victim creates a ticket with a known key.
    await tool_harness.invoke(
        organization_id=uuid.UUID(victim_org),
        tool_name="ticketing.create_ticket",
        args={"subject": "victim secret", "body": "x"},
        idempotency_key="shared-key",
    )

    attacker = await api.user()
    attacker_org = await api.org(attacker)
    # Not installed for the attacker: refused before anything runs.
    with pytest.raises(ToolNotEnabled):
        await tool_harness.invoke(
            organization_id=uuid.UUID(attacker_org),
            tool_name="crm.get_customer",
            args={"customer_ref": "C-1001"},
        )
    # Installed but with no data of their own: the victim's C-1001 is invisible.
    await client.put(
        f"/api/v1/orgs/{attacker_org}/tools/crm.get_customer", json={}, headers=attacker.headers
    )
    with pytest.raises(ToolBusinessError):
        await tool_harness.invoke(
            organization_id=uuid.UUID(attacker_org),
            tool_name="crm.get_customer",
            args={"customer_ref": "C-1001"},
        )
    # Reusing the victim's idempotency key does not replay the victim's result.
    await client.put(
        f"/api/v1/orgs/{attacker_org}/tools/ticketing.create_ticket",
        json={},
        headers=attacker.headers,
    )
    inv = await tool_harness.invoke(
        organization_id=uuid.UUID(attacker_org),
        tool_name="ticketing.create_ticket",
        args={"subject": "mine", "body": "y"},
        idempotency_key="shared-key",
    )
    assert inv.replayed is False and inv.output["created"] is True


async def test_tool_routes_are_tenant_isolated(
    api: Api, client: AsyncClient, tool_harness: ToolExecutor
) -> None:
    _, victim_org = await _seeded(api, client)
    attacker = await api.user()
    attacker_org = await api.org(attacker)
    for method, path, body in [
        ("GET", "/tools", None),
        ("PUT", "/tools/email.send_message", {"credentials": {"api_key": "x"}}),
        ("GET", "/tool-calls", None),
        ("POST", "/demo-data", None),
        ("GET", "/simulated/activity", None),
    ]:
        r = await client.request(
            method, f"/api/v1/orgs/{victim_org}{path}", json=body, headers=attacker.headers
        )
        assert r.status_code == 404, path
    own = (
        await client.get(
            f"/api/v1/orgs/{attacker_org}/simulated/activity", headers=attacker.headers
        )
    ).json()
    assert own == {"tickets": [], "messages": [], "refunds": []}


ROLES = ["viewer", "operator", "admin", "owner"]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    ("method", "path", "body", "min_role"),
    [
        ("GET", "/tools", None, "viewer"),
        ("GET", "/tool-calls", None, "viewer"),
        ("GET", "/simulated/activity", None, "viewer"),
        ("PUT", "/tools/crm.get_customer", {"enabled": True}, "admin"),
        ("PUT", "/tools/email.send_message", {"credentials": {"api_key": "k"}}, "admin"),
        ("POST", "/demo-data", None, "admin"),
    ],
)
async def test_tool_rbac(
    api: Api,
    client: AsyncClient,
    tool_harness: ToolExecutor,
    role: str,
    method: str,
    path: str,
    body: dict | None,
    min_role: str,  # type: ignore[type-arg]
) -> None:
    owner = await api.user()
    org = await api.org(owner)
    actor = owner if role == "owner" else await api.member(org, owner, role)
    r = await client.request(method, f"/api/v1/orgs/{org}{path}", json=body, headers=actor.headers)
    expected_ok = ROLES.index(role) >= ROLES.index(min_role)
    assert r.status_code == (200 if expected_ok else 403), r.text


def test_production_requires_credentials_key() -> None:
    from solutionforge.core.config import Environment, Settings

    with pytest.raises(ValueError, match="SF_CREDENTIALS_KEYS"):
        Settings(environment=Environment.PRODUCTION, jwt_secret="x" * 40)  # type: ignore[arg-type]
