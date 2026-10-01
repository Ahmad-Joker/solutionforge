"""Phase 7: brute-force limits, initiator-aware tool authorization, org tool policy."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from solutionforge.tools.executor import ToolExecutor
from solutionforge.workflows.engine import Engine
from tests.helpers import DEFAULT_PASSWORD, Api
from tests.workflow_support import get_exec, make_workflow, start, step

pytestmark = pytest.mark.security


# ------------------------------------------------------------------ rate limiting


async def test_login_brute_force_on_one_account_is_throttled(api: Api, client: AsyncClient) -> None:
    await api.register("target@example.com")
    codes = [(await api.login("target@example.com", f"guess-{i}")).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
    r = await api.login("target@example.com", DEFAULT_PASSWORD)  # even the right password
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0 and r.json()["error"]["code"] == "rate_limited"
    # Other accounts are unaffected by one account's lockout window.
    await api.register("other@example.com")
    assert (await api.login("other@example.com")).status_code == 200


async def test_password_spraying_from_one_ip_is_throttled(api: Api, client: AsyncClient) -> None:
    codes = [
        (
            await client.post(
                "/api/v1/auth/login", json={"email": f"u{i}@example.com", "password": "x"}
            )
        ).status_code
        for i in range(52)
    ]
    assert codes.count(401) == 50 and codes[-2:] == [429, 429]


async def test_refresh_endpoint_is_throttled(app, client: AsyncClient) -> None:  # type: ignore[no-untyped-def]
    codes = [
        (await client.post("/api/v1/auth/refresh", json={"refresh_token": "x" * 43})).status_code
        for _ in range(122)
    ]
    assert codes.count(401) == 120 and codes[-1] == 429


async def test_rate_limiting_can_be_disabled(api: Api, client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    app.state.settings.rate_limit_enabled = False
    codes = {(await api.login("nobody@example.com", "x")).status_code for _ in range(15)}
    assert codes == {401}


# ------------------------------------------------------------------ initiator-aware tool policy


TOOL_WF = {
    "start": "lookup",
    "steps": [
        step("lookup", "tool", {"tool": "crm.get_customer", "args": {"customer_ref": "C-1001"}})
    ],
}


async def _seeded(api: Api, client: AsyncClient):  # type: ignore[no-untyped-def]
    s = await make_workflow(api, TOOL_WF)
    assert (await client.post(s.url("/demo-data"), headers=s.owner.headers)).status_code == 200
    return s


async def test_demoted_initiator_loses_tool_access_before_execution_runs(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor
) -> None:
    s = await _seeded(api, client)
    operator = await api.member(s.org, s.owner, "operator")
    r = await client.post(
        s.url(f"/workflows/{s.workflow}/executions"), json={}, headers=operator.headers
    )
    eid = r.json()["id"]
    # Demoted after starting, before the worker picks it up.
    await client.patch(
        s.url(f"/members/{operator.user_id}"), json={"role": "viewer"}, headers=s.owner.headers
    )
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed" and ex["error"]["code"] == "tool_denied"
    assert "lacks 'workflow:execute'" in ex["error"]["message"]


async def test_removed_initiator_cannot_act_through_running_workflows(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor
) -> None:
    s = await _seeded(api, client)
    operator = await api.member(s.org, s.owner, "operator")
    eid = (
        await client.post(
            s.url(f"/workflows/{s.workflow}/executions"), json={}, headers=operator.headers
        )
    ).json()["id"]
    await client.delete(s.url(f"/members/{operator.user_id}"), headers=s.owner.headers)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["error"]["code"] == "tool_denied" and "no longer a member" in ex["error"]["message"]


async def test_org_policy_blocks_tools_and_is_audited(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor
) -> None:
    s = await _seeded(api, client)
    r = await client.put(
        s.url("/tool-policy"), json={"blocked_tools": ["crm.get_customer"]}, headers=s.owner.headers
    )
    assert r.status_code == 200 and r.json()["policy"]["blocked_tools"] == ["crm.get_customer"]
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert (
        ex["error"]["code"] == "tool_denied"
        and "organization tool policy" in ex["error"]["message"]
    )
    audit = (
        await client.get(
            s.url("/audit-events"),
            params={"event_type": "org.policy_updated"},
            headers=s.owner.headers,
        )
    ).json()
    assert audit and audit[0]["metadata"]["after"]["blocked_tools"] == ["crm.get_customer"]


async def test_org_policy_validation_and_rbac(
    api: Api, client: AsyncClient, tool_harness: ToolExecutor
) -> None:
    s = await _seeded(api, client)
    assert (
        await client.put(
            s.url("/tool-policy"), json={"blocked_tools": ["shell.exec"]}, headers=s.owner.headers
        )
    ).status_code == 422
    assert (
        await client.put(s.url("/tool-policy"), json={"allow_all": True}, headers=s.owner.headers)
    ).status_code == 422
    viewer = await api.member(s.org, s.owner, "viewer")
    admin = await api.member(s.org, s.owner, "admin")
    assert (await client.get(s.url("/tool-policy"), headers=viewer.headers)).status_code == 200
    assert (
        await client.put(s.url("/tool-policy"), json={}, headers=viewer.headers)
    ).status_code == 403
    assert (
        await client.put(
            s.url("/tool-policy"), json={"auto_allow_up_to": "read_only"}, headers=admin.headers
        )
    ).status_code == 200
