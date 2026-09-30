from __future__ import annotations

import asyncio
import uuid

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.domain import AuditEvent
from solutionforge.workflows.engine import Engine
from tests.helpers import Api
from tests.workflow_support import get_exec, make_workflow, start, step

SIMPLE = {"start": "a", "steps": [step("a", "transform", {"set": {"v": 1}})]}

APPROVAL = {
    "start": "draft",
    "inputs": {"customer": {"type": "string"}},
    "steps": [
        step(
            "draft", "transform", {"set": {"email": "Dear {{ $.input.customer }}"}}, next="review"
        ),
        step(
            "review",
            "approval",
            {"message": "Send email to {{ $.input.customer }}?", "on_reject": "log_rejection"},
            next="send",
        ),
        step("send", "transform", {"set": {"sent": True}}),
        step("log_rejection", "transform", {"set": {"sent": False}}),
    ],
    "output": {"sent": "$.state.sent"},
}


# ------------------------------------------------------------------ versions & deployments


async def test_invalid_definition_returns_structured_422(api: Api, client: AsyncClient) -> None:
    s = await make_workflow(api, SIMPLE)
    bad = {"start": "a", "steps": [step("a", "transform", next="ghost"), step("z", "nope")]}
    r = await client.post(
        s.url(f"/workflows/{s.workflow}/versions"),
        json={"definition": bad},
        headers=s.owner.headers,
    )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "invalid_workflow_definition"
    msgs = " | ".join(e["msg"] for e in body["details"]["errors"])
    assert "unknown step 'ghost'" in msgs and "unknown step type 'nope'" in msgs


async def test_versions_are_numbered_and_immutable(api: Api, client: AsyncClient) -> None:
    s = await make_workflow(api, SIMPLE)
    r = await client.post(
        s.url(f"/workflows/{s.workflow}/versions"),
        json={"definition": APPROVAL, "changelog": "add approval"},
        headers=s.owner.headers,
    )
    assert r.status_code == 201 and r.json()["version"] == 2
    versions = (
        await client.get(s.url(f"/workflows/{s.workflow}/versions"), headers=s.owner.headers)
    ).json()
    assert [v["version"] for v in versions] == [2, 1]
    # No update/delete routes exist for versions.
    for method in ("PUT", "PATCH", "DELETE"):
        r = await client.request(
            method, s.url(f"/workflows/{s.workflow}/versions/1"), headers=s.owner.headers
        )
        assert r.status_code == 405


async def test_new_version_is_not_live_until_deployed_and_rollback_works(
    api: Api, client: AsyncClient, engine: Engine
) -> None:
    v1 = {
        "start": "a",
        "steps": [step("a", "transform", {"set": {"v": 1}})],
        "output": {"v": "$.state.v"},
    }
    v2 = {
        "start": "a",
        "steps": [step("a", "transform", {"set": {"v": 2}})],
        "output": {"v": "$.state.v"},
    }
    s = await make_workflow(api, v1)
    await client.post(
        s.url(f"/workflows/{s.workflow}/versions"), json={"definition": v2}, headers=s.owner.headers
    )

    async def run_output() -> object:
        eid = await start(api, s)
        await engine.run_until_idle()
        return (await get_exec(api, s, eid))["output"]["v"]

    assert await run_output() == 1  # v2 exists but v1 is still production
    deploy = s.url(f"/workflows/{s.workflow}/deployments")
    assert (
        await client.post(deploy, json={"version": 2}, headers=s.owner.headers)
    ).status_code == 201
    assert await run_output() == 2
    r = await client.post(
        deploy, json={"version": 1, "reason": "rollback"}, headers=s.owner.headers
    )
    assert r.status_code == 201
    assert await run_output() == 1
    assert (
        await client.post(deploy, json={"version": 1}, headers=s.owner.headers)
    ).status_code == 409

    history = (await client.get(deploy, headers=s.owner.headers)).json()
    assert [(d["version"], d["reason"]) for d in history] == [(1, "rollback"), (2, ""), (1, "")]
    wf = (await client.get(s.url(f"/workflows/{s.workflow}"), headers=s.owner.headers)).json()
    assert wf["deployed_version"] == 1 and wf["latest_version"] == 2


async def test_execution_requires_deployment_or_explicit_version(
    api: Api, client: AsyncClient
) -> None:
    s = await make_workflow(api, SIMPLE, deploy=False)
    url = s.url(f"/workflows/{s.workflow}/executions")
    assert (await client.post(url, json={}, headers=s.owner.headers)).status_code == 409
    assert (await client.post(url, json={"version": 1}, headers=s.owner.headers)).status_code == 202
    assert (await client.post(url, json={"version": 7}, headers=s.owner.headers)).status_code == 404


async def test_operator_can_run_production_but_not_unreleased_versions(
    api: Api, client: AsyncClient
) -> None:
    s = await make_workflow(api, SIMPLE)
    await client.post(
        s.url(f"/workflows/{s.workflow}/versions"),
        json={"definition": SIMPLE},
        headers=s.owner.headers,
    )
    operator = await api.member(s.org, s.owner, "operator")
    url = s.url(f"/workflows/{s.workflow}/executions")
    assert (await client.post(url, json={}, headers=operator.headers)).status_code == 202
    assert (
        await client.post(url, json={"version": 1}, headers=operator.headers)
    ).status_code == 202
    r = await client.post(url, json={"version": 2}, headers=operator.headers)
    assert r.status_code == 403


async def test_duplicate_workflow_name_conflicts(api: Api, client: AsyncClient) -> None:
    s = await make_workflow(api, SIMPLE)
    r = await client.post(s.url("/workflows"), json={"name": "wf"}, headers=s.owner.headers)
    assert r.status_code == 409


# ------------------------------------------------------------------ execution creation


async def test_input_validation(api: Api, client: AsyncClient) -> None:
    s = await make_workflow(api, APPROVAL)
    url = s.url(f"/workflows/{s.workflow}/executions")
    for bad in ({}, {"customer": 5}, {"customer": "x", "admin": True}):
        r = await client.post(url, json={"input": bad}, headers=s.owner.headers)
        assert r.status_code == 422, bad


async def test_oversized_input_rejected(api: Api, client: AsyncClient) -> None:
    s = await make_workflow(api, SIMPLE)
    r = await client.post(
        s.url(f"/workflows/{s.workflow}/executions"),
        json={"input": {"blob": "x" * 300_000}},
        headers=s.owner.headers,
    )
    assert r.status_code == 422


async def test_idempotency_key_returns_original_execution(api: Api, client: AsyncClient) -> None:
    s = await make_workflow(api, SIMPLE)
    url = s.url(f"/workflows/{s.workflow}/executions")
    body = {"idempotency_key": "order-1234-retry"}
    first = await client.post(url, json=body, headers=s.owner.headers)
    again = await client.post(url, json=body, headers=s.owner.headers)
    assert (first.status_code, again.status_code) == (202, 200)
    assert first.json()["id"] == again.json()["id"]

    other = await client.post(s.url("/workflows"), json={"name": "other"}, headers=s.owner.headers)
    r = await client.post(
        s.url(f"/workflows/{other.json()['id']}/executions"), json=body, headers=s.owner.headers
    )
    assert r.status_code == 409


async def test_concurrent_idempotent_requests_create_one_execution(
    api: Api, client: AsyncClient
) -> None:
    s = await make_workflow(api, SIMPLE)
    url = s.url(f"/workflows/{s.workflow}/executions")
    results = await asyncio.gather(
        *[
            client.post(url, json={"idempotency_key": "same-key-123"}, headers=s.owner.headers)
            for _ in range(5)
        ]
    )
    assert {r.status_code for r in results} <= {200, 202}
    assert len({r.json()["id"] for r in results}) == 1


async def test_list_executions_filters(api: Api, client: AsyncClient, engine: Engine) -> None:
    s = await make_workflow(api, SIMPLE)
    await start(api, s)
    await engine.run_until_idle()
    await start(api, s)
    listed = (
        await client.get(s.url("/executions"), params={"status": "queued"}, headers=s.owner.headers)
    ).json()
    assert len(listed) == 1
    listed = (
        await client.get(
            s.url("/executions"), params={"workflow_id": s.workflow}, headers=s.owner.headers
        )
    ).json()
    assert len(listed) == 2


# ------------------------------------------------------------------ human-in-the-loop


async def test_approval_suspends_and_resumes(
    api: Api, client: AsyncClient, engine: Engine, db: AsyncSession
) -> None:
    s = await make_workflow(api, APPROVAL)
    eid = await start(api, s, {"customer": "Ada"})
    await engine.run_until_idle()

    ex = await get_exec(api, s, eid)
    assert ex["status"] == "waiting"
    assert ex["waiting_on"] == {"reason": "approval", "message": "Send email to Ada?"}
    assert await engine.claim() is None  # a waiting execution holds no worker

    r = await client.post(
        s.url(f"/executions/{eid}/resume"),
        json={"payload": {"approved": True, "comment": "looks good"}},
        headers=s.owner.headers,
    )
    assert r.status_code == 200 and r.json()["status"] == "queued"
    await engine.run_until_idle()

    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded" and ex["output"] == {"sent": True}
    assert [(st["step_id"], st["status"]) for st in ex["steps"]] == [
        ("draft", "succeeded"),
        ("review", "waiting"),
        ("review", "resumed"),
        ("send", "succeeded"),
    ]
    assert await db.scalar(
        sa.select(AuditEvent.id).where(AuditEvent.event_type == "execution.resumed")
    )


async def test_rejection_routes_to_on_reject(api: Api, client: AsyncClient, engine: Engine) -> None:
    s = await make_workflow(api, APPROVAL)
    eid = await start(api, s, {"customer": "Bob"})
    await engine.run_until_idle()
    await client.post(
        s.url(f"/executions/{eid}/resume"),
        json={"payload": {"approved": False}},
        headers=s.owner.headers,
    )
    await engine.run_until_idle()
    assert (await get_exec(api, s, eid))["output"] == {"sent": False}


async def test_rejection_without_handler_fails(
    api: Api, client: AsyncClient, engine: Engine
) -> None:
    s = await make_workflow(
        api, {"start": "r", "steps": [step("r", "approval", {"message": "ok?"})]}
    )
    eid = await start(api, s)
    await engine.run_until_idle()
    r = await client.post(
        s.url(f"/executions/{eid}/resume"),
        json={"payload": {"approved": False}},
        headers=s.owner.headers,
    )
    assert r.json()["status"] == "failed" and r.json()["error"]["code"] == "rejected"


async def test_resume_guards(api: Api, client: AsyncClient, engine: Engine) -> None:
    s = await make_workflow(api, APPROVAL)
    eid = await start(api, s, {"customer": "Cy"})
    url = s.url(f"/executions/{eid}/resume")
    # Not waiting yet.
    assert (
        await client.post(url, json={"payload": {"approved": True}}, headers=s.owner.headers)
    ).status_code == 409
    await engine.run_until_idle()
    # Malformed decision leaves the execution untouched.
    r = await client.post(
        url, json={"payload": {"approved": "yes please"}}, headers=s.owner.headers
    )
    assert r.status_code == 422
    assert (await get_exec(api, s, eid))["status"] == "waiting"
    # Viewers cannot decide.
    viewer = await api.member(s.org, s.owner, "viewer")
    assert (
        await client.post(url, json={"payload": {"approved": True}}, headers=viewer.headers)
    ).status_code == 403
    # Double resume: only the first counts.
    first = await client.post(url, json={"payload": {"approved": True}}, headers=s.owner.headers)
    second = await client.post(url, json={"payload": {"approved": False}}, headers=s.owner.headers)
    assert (first.status_code, second.status_code) == (200, 409)


# ------------------------------------------------------------------ cancellation


async def test_cancel_queued_and_waiting(api: Api, client: AsyncClient, engine: Engine) -> None:
    s = await make_workflow(api, APPROVAL)
    queued = await start(api, s, {"customer": "A"})
    r = await client.post(s.url(f"/executions/{queued}/cancel"), headers=s.owner.headers)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert await engine.run_until_idle() == 0

    waiting = await start(api, s, {"customer": "B"})
    await engine.run_until_idle()
    r = await client.post(s.url(f"/executions/{waiting}/cancel"), headers=s.owner.headers)
    assert r.json()["status"] == "cancelled" and r.json()["waiting_on"] is None
    # Terminal executions cannot be cancelled again.
    assert (
        await client.post(s.url(f"/executions/{waiting}/cancel"), headers=s.owner.headers)
    ).status_code == 409


async def test_cancel_running_stops_before_next_step(
    api: Api, client: AsyncClient, engine: Engine
) -> None:
    s = await make_workflow(
        api, {"start": "a", "steps": [step("a", "transform", next="b"), step("b", "transform")]}
    )
    eid = await start(api, s)
    assert await engine.claim() is not None  # a worker holds it (status running)
    r = await client.post(s.url(f"/executions/{eid}/cancel"), headers=s.owner.headers)
    assert r.json()["status"] == "running" and r.json()["cancel_requested"] is True
    await engine.run(uuid.UUID(eid))
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "cancelled" and ex["steps"] == []
