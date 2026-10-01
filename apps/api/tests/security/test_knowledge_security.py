"""Knowledge bases: tenant isolation (API + retrieval + workflow steps) and RBAC."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from solutionforge.llm.providers.mock import MockProvider
from solutionforge.retrieval.embeddings import HashingEmbedder
from solutionforge.retrieval.ingest import IngestionWorker
from solutionforge.workflows.engine import Engine
from tests.helpers import Api, Session
from tests.workflow_support import get_exec, make_workflow, start, step

pytestmark = pytest.mark.security

SECRET = "Project Nightingale merger closes on March 3; codename BLUEJAY."


async def _kb(
    api: Api, client: AsyncClient, owner: Session, org: str, content: str
) -> tuple[str, str]:
    kb = (
        await client.post(
            f"/api/v1/orgs/{org}/knowledge-bases", json={"name": "docs"}, headers=owner.headers
        )
    ).json()["id"]
    doc = (
        await client.post(
            f"/api/v1/orgs/{org}/knowledge-bases/{kb}/documents",
            json={"title": "Internal", "content": content},
            headers=owner.headers,
        )
    ).json()["id"]
    return kb, doc


async def test_knowledge_is_tenant_isolated(
    api: Api, client: AsyncClient, app: FastAPI, engine: Engine, mock_llm: MockProvider
) -> None:
    victim = await api.user()
    victim_org = await api.org(victim)
    victim_kb, victim_doc = await _kb(api, client, victim, victim_org, SECRET)

    # Attacker has a KB with the SAME name in their own org, used by their workflow.
    wf = await make_workflow(
        api,
        {
            "start": "find",
            "steps": [
                step("find", "retrieve", {"knowledge_base": "docs", "query": "merger codename"})
            ],
            "output": {"chunks": "$.steps.find.chunks"},
        },
    )
    await _kb(api, client, wf.owner, wf.org, "Public FAQ about shipping and returns.")
    await IngestionWorker(app.state.sessionmaker, HashingEmbedder(), worker_id="i").run_until_idle()

    eid = await start(api, wf)
    await engine.run_until_idle()
    out = str((await get_exec(api, wf, eid))["output"])
    assert (
        "BLUEJAY" not in out and "Nightingale" not in out
    )  # same KB name, other tenant: invisible

    a = wf.owner
    for method, path, body in [
        ("GET", f"/knowledge-bases/{victim_kb}", None),
        ("POST", f"/knowledge-bases/{victim_kb}/search", {"query": "merger"}),
        ("GET", f"/knowledge-bases/{victim_kb}/documents", None),
        ("POST", f"/knowledge-bases/{victim_kb}/documents", {"title": "x", "content": "poison"}),
        ("DELETE", f"/knowledge-bases/{victim_kb}", None),
        ("GET", f"/documents/{victim_doc}", None),
        ("DELETE", f"/documents/{victim_doc}", None),
        ("POST", f"/documents/{victim_doc}/retry", None),
    ]:
        # Victim's org in the path: membership check fails.
        r = await client.request(
            method, f"/api/v1/orgs/{victim_org}{path}", json=body, headers=a.headers
        )
        assert r.status_code == 404, path
        # Attacker's own org + victim's resource ids (confused deputy): still not found.
        r = await client.request(
            method, f"/api/v1/orgs/{wf.org}{path}", json=body, headers=a.headers
        )
        assert r.status_code == 404, path

    hits = (
        await client.post(
            f"/api/v1/orgs/{victim_org}/knowledge-bases/{victim_kb}/search",
            json={"query": "merger codename"},
            headers=victim.headers,
        )
    ).json()["results"]
    assert hits and "BLUEJAY" in hits[0]["text"]  # victim's data intact and searchable by victim


ROLES = ["viewer", "operator", "admin", "owner"]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    ("method", "path", "body", "min_role"),
    [
        ("GET", "/knowledge-bases", None, "viewer"),
        ("POST", "/knowledge-bases/{kb}/search", {"query": "refund"}, "viewer"),
        ("GET", "/knowledge-bases/{kb}/documents", None, "viewer"),
        ("POST", "/knowledge-bases", {"name": "another"}, "admin"),
        (
            "POST",
            "/knowledge-bases/{kb}/documents",
            {"title": "t", "content": "new content"},
            "admin",
        ),
        ("DELETE", "/documents/{doc}", None, "admin"),
    ],
)
async def test_knowledge_rbac(
    api: Api,
    client: AsyncClient,
    role: str,
    method: str,
    path: str,
    body: dict | None,
    min_role: str,  # type: ignore[type-arg]
) -> None:
    owner = await api.user()
    org = await api.org(owner)
    kb, doc = await _kb(api, client, owner, org, "Refunds take 5 days.")
    actor = owner if role == "owner" else await api.member(org, owner, role)
    r = await client.request(
        method,
        f"/api/v1/orgs/{org}" + path.format(kb=kb, doc=doc),
        json=body,
        headers=actor.headers,
    )
    ok = ROLES.index(role) >= ROLES.index(min_role)
    assert (r.status_code < 300) is ok, (role, path, r.status_code, r.text)
    if not ok:
        assert r.status_code == 403
