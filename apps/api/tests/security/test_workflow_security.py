"""Tenant isolation and RBAC for workflows, versions, deployments and executions."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from httpx import AsyncClient

from solutionforge.workflows.engine import Engine
from tests.helpers import Api, Session
from tests.workflow_support import WorkflowSetup, get_exec, make_workflow, start, step

pytestmark = pytest.mark.security

APPROVAL = {"start": "r", "steps": [step("r", "approval", {"message": "ok?"})]}


@dataclass
class Victim:
    setup: WorkflowSetup
    execution: str
    attacker: Session
    attacker_org: str


@pytest.fixture
async def victim(api: Api, engine: Engine) -> Victim:
    s = await make_workflow(api, APPROVAL)
    eid = await start(api, s)
    await engine.run_until_idle()  # victim's execution is now waiting for approval
    attacker = await api.user()
    attacker_org = await api.org(attacker, "Evil Corp")
    return Victim(s, eid, attacker, attacker_org)


ROUTES = [
    ("GET", "/workflows/{wf}", None),
    ("GET", "/workflows/{wf}/versions", None),
    ("GET", "/workflows/{wf}/versions/1", None),
    ("POST", "/workflows/{wf}/versions", {"definition": APPROVAL}),
    ("GET", "/workflows/{wf}/deployments", None),
    ("POST", "/workflows/{wf}/deployments", {"version": 1}),
    ("POST", "/workflows/{wf}/executions", {}),
    ("GET", "/executions/{ex}", None),
    ("POST", "/executions/{ex}/cancel", None),
    ("POST", "/executions/{ex}/resume", {"payload": {"approved": True}}),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_victim_org_in_path_is_404(
    client: AsyncClient,
    victim: Victim,
    method: str,
    path: str,
    body: dict | None,  # type: ignore[type-arg]
) -> None:
    url = f"/api/v1/orgs/{victim.setup.org}" + path.format(
        wf=victim.setup.workflow, ex=victim.execution
    )
    r = await client.request(method, url, json=body, headers=victim.attacker.headers)
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "Organization not found"


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_own_org_path_with_victim_resource_ids_is_404(
    client: AsyncClient,
    victim: Victim,
    method: str,
    path: str,
    body: dict | None,  # type: ignore[type-arg]
) -> None:
    """Confused deputy: attacker's own org (where they are OWNER) + victim's resource IDs."""
    url = f"/api/v1/orgs/{victim.attacker_org}" + path.format(
        wf=victim.setup.workflow, ex=victim.execution
    )
    r = await client.request(method, url, json=body, headers=victim.attacker.headers)
    assert r.status_code == 404, (url, r.text)


async def test_victim_execution_untouched_after_attacks(
    api: Api, client: AsyncClient, victim: Victim
) -> None:
    for method, path, body in ROUTES:
        url = f"/api/v1/orgs/{victim.attacker_org}" + path.format(
            wf=victim.setup.workflow, ex=victim.execution
        )
        await client.request(method, url, json=body, headers=victim.attacker.headers)
    ex = await get_exec(api, victim.setup, victim.execution)
    assert ex["status"] == "waiting" and len(ex["steps"]) == 1


async def test_listings_do_not_leak(client: AsyncClient, victim: Victim) -> None:
    base = f"/api/v1/orgs/{victim.attacker_org}"
    assert (await client.get(f"{base}/workflows", headers=victim.attacker.headers)).json() == []
    assert (await client.get(f"{base}/executions", headers=victim.attacker.headers)).json() == []
    r = await client.get(
        f"{base}/executions",
        params={"workflow_id": victim.setup.workflow},
        headers=victim.attacker.headers,
    )
    assert r.json() == []


async def test_idempotency_keys_are_per_tenant(api: Api, client: AsyncClient) -> None:
    """The same key in two orgs must create two executions, not return the other tenant's."""
    a = await make_workflow(api, APPROVAL, name="wf-a")
    b = await make_workflow(api, APPROVAL, name="wf-b")
    body = {"idempotency_key": "shared-key-0001"}
    ra = await client.post(
        a.url(f"/workflows/{a.workflow}/executions"), json=body, headers=a.owner.headers
    )
    rb = await client.post(
        b.url(f"/workflows/{b.workflow}/executions"), json=body, headers=b.owner.headers
    )
    assert (ra.status_code, rb.status_code) == (202, 202)
    assert ra.json()["id"] != rb.json()["id"]


# ------------------------------------------------------------------ RBAC matrix

ROLES = ["viewer", "operator", "admin", "owner"]
MATRIX = [
    ("GET", "/workflows", None, "viewer"),
    ("GET", "/workflows/{wf}/versions", None, "viewer"),
    ("GET", "/executions/{ex}", None, "viewer"),
    ("POST", "/workflows/{wf}/executions", {}, "operator"),
    ("POST", "/executions/{ex}/resume", {"payload": {"approved": True}}, "operator"),
    ("POST", "/executions/{ex}/cancel", None, "operator"),
    ("POST", "/workflows", {"name": "new-workflow"}, "admin"),
    ("POST", "/workflows/{wf}/versions", {"definition": APPROVAL}, "admin"),
    ("POST", "/workflows/{wf}/deployments", {"version": 2}, "admin"),
]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(("method", "path", "body", "min_role"), MATRIX)
async def test_workflow_rbac_matrix(
    api: Api,
    client: AsyncClient,
    engine: Engine,
    role: str,
    method: str,
    path: str,
    body: dict | None,  # type: ignore[type-arg]
    min_role: str,
) -> None:
    s = await make_workflow(api, APPROVAL)
    # A second version so the deploy case has something new to deploy.
    await client.post(
        s.url(f"/workflows/{s.workflow}/versions"),
        json={"definition": APPROVAL},
        headers=s.owner.headers,
    )
    eid = await start(api, s)
    await engine.run_until_idle()
    actor = s.owner if role == "owner" else await api.member(s.org, s.owner, role)

    r = await client.request(
        method, s.url(path.format(wf=s.workflow, ex=eid)), json=body, headers=actor.headers
    )
    if ROLES.index(role) >= ROLES.index(min_role):
        assert r.status_code in (200, 201, 202), r.text
    else:
        assert r.status_code == 403, r.text
