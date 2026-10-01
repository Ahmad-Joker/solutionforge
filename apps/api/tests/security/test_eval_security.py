"""Evaluation and deployment-gate authorization: who may define, run, gate, and override."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.helpers import Api
from tests.integration.test_evaluation import V1, eval_setup
from tests.workflow_support import make_workflow

pytestmark = pytest.mark.security


async def test_role_matrix_for_evaluation(api: Api, client: AsyncClient) -> None:
    s, ds = await eval_setup(api, client)
    viewer = await api.member(s.org, s.owner, "viewer")
    operator = await api.member(s.org, s.owner, "operator")
    admin = await api.member(s.org, s.owner, "admin")

    # Reading results is open to every member (viewers included).
    r = await client.get(s.url(f"/evaluation/datasets/{ds}/cases"), headers=viewer.headers)
    assert r.status_code == 200 and len(r.json()) == 3

    # Defining datasets/cases is a workflow-authoring action.
    for who in (viewer, operator):
        r = await client.post(
            s.url("/evaluation/datasets"),
            json={"workflow_id": s.workflow, "name": "mine"},
            headers=who.headers,
        )
        assert r.status_code == 403
        r = await client.post(
            s.url(f"/evaluation/datasets/{ds}/cases"),
            json={"name": "x", "expectations": {}},
            headers=who.headers,
        )
        assert r.status_code == 403

    # Viewers cannot spend money running evaluations at all.
    r = await client.post(
        s.url(f"/evaluation/datasets/{ds}/runs"), json={"version": 1}, headers=viewer.headers
    )
    assert r.status_code == 403
    # Operators may evaluate production, but not unreleased versions (same rule as runs).
    r = await client.post(
        s.url(f"/evaluation/datasets/{ds}/runs"), json={"version": 1}, headers=operator.headers
    )
    assert r.status_code == 202
    r = await client.post(
        s.url(f"/evaluation/datasets/{ds}/runs"), json={"version": 2}, headers=operator.headers
    )
    assert r.status_code == 403

    # Gate policy is a deploy-level control.
    for who in (viewer, operator):
        r = await client.put(
            s.url(f"/workflows/{s.workflow}/gate"), json={"dataset_id": ds}, headers=who.headers
        )
        assert r.status_code == 403
    r = await client.put(
        s.url(f"/workflows/{s.workflow}/gate"), json={"dataset_id": ds}, headers=admin.headers
    )
    assert r.status_code == 200


async def test_cross_tenant_evaluation_resources_are_invisible(
    api: Api, client: AsyncClient
) -> None:
    s, ds = await eval_setup(api, client)
    run = (
        await client.post(
            s.url(f"/evaluation/datasets/{ds}/runs"), json={"version": 1}, headers=s.owner.headers
        )
    ).json()["id"]
    attacker = await make_workflow(api, V1)  # owner of a different org
    a = attacker.owner.headers

    def theirs(path: str) -> str:
        return attacker.url(path)

    # Victim's IDs through the attacker's own org: indistinguishable from nonexistent.
    for path in (
        f"/evaluation/datasets/{ds}/cases",
        f"/evaluation/datasets/{ds}/runs",
        f"/evaluation/runs/{run}",
    ):
        assert (await client.get(theirs(path), headers=a)).status_code == 404
    r = await client.get(theirs("/evaluation/compare"), params={"run_id": [run]}, headers=a)
    assert r.status_code == 404
    r = await client.post(theirs(f"/evaluation/datasets/{ds}/runs"), json={"version": 1}, headers=a)
    assert r.status_code == 404
    r = await client.post(
        theirs("/evaluation/datasets"), json={"workflow_id": s.workflow, "name": "x"}, headers=a
    )
    assert r.status_code == 404
    # Pointing your own gate at someone else's dataset is also a 404, not a leak.
    r = await client.put(
        theirs(f"/workflows/{attacker.workflow}/gate"), json={"dataset_id": ds}, headers=a
    )
    assert r.status_code == 404
    assert (await client.get(theirs("/evaluation/datasets"), headers=a)).json() == []
    # And the victim's org rejects the attacker outright.
    assert (await client.get(s.url("/evaluation/datasets"), headers=a)).status_code in (403, 404)


async def test_gate_dataset_must_belong_to_the_workflow_and_inputs_are_validated(
    api: Api, client: AsyncClient
) -> None:
    s, ds = await eval_setup(api, client)
    r = await client.post(s.url("/workflows"), json={"name": "other"}, headers=s.owner.headers)
    other = r.json()["id"]
    r = await client.put(
        s.url(f"/workflows/{other}/gate"), json={"dataset_id": ds}, headers=s.owner.headers
    )
    assert r.status_code == 422
    r = await client.put(
        s.url(f"/workflows/{s.workflow}/gate"),
        json={"dataset_id": ds, "min_pass_rate": 2},
        headers=s.owner.headers,
    )
    assert r.status_code == 422
    r = await client.put(
        s.url(f"/workflows/{s.workflow}/gate"),
        json={"dataset_id": ds, "disable_all_checks": True},
        headers=s.owner.headers,
    )
    assert r.status_code == 422  # unknown policy keys are rejected, not ignored
    bad_exp = [
        {"status": "exploded"},
        {"output_schema": {"type": "not-a-type"}},
        {"grade_with_llm": True},
    ]
    for exp in bad_exp:
        r = await client.post(
            s.url(f"/evaluation/datasets/{ds}/cases"),
            json={"name": "bad", "expectations": exp},
            headers=s.owner.headers,
        )
        assert r.status_code == 422, exp
    r = await client.post(
        s.url(f"/evaluation/datasets/{ds}/cases"),
        json={"name": "gold lookup", "expectations": {}},
        headers=s.owner.headers,
    )
    assert r.status_code == 409
    r = await client.post(
        s.url("/evaluation/datasets"),
        json={"workflow_id": s.workflow, "name": "regression"},
        headers=s.owner.headers,
    )
    assert r.status_code == 409
    r = await client.post(
        s.url("/evaluation/datasets"),
        json={"workflow_id": s.workflow, "name": "empty"},
        headers=s.owner.headers,
    )
    empty = r.json()["id"]
    r = await client.post(
        s.url(f"/evaluation/datasets/{empty}/runs"), json={"version": 1}, headers=s.owner.headers
    )
    assert r.status_code == 422
