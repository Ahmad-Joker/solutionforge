"""Human-in-the-loop approvals for policy-gated tool calls."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.domain import Approval, AuditEvent, SimMessage, SimRefund, ToolCall
from solutionforge.services.approval_service import expire_due
from solutionforge.tools.executor import ToolExecutor
from solutionforge.workflows.engine import Engine
from tests.helpers import Api, Session
from tests.workflow_support import WorkflowSetup, get_exec, make_workflow, start, step

EMAIL_WF = {
    "start": "draft",
    "inputs": {"to": {"type": "string"}},
    "steps": [
        step(
            "draft",
            "tool",
            {
                "tool": "email.draft_message",
                "args": {"to": "$.input.to", "subject": "Your order", "body": "Sorry!"},
            },
            next="send",
        ),
        step(
            "send",
            "tool",
            {
                "tool": "email.send_message",
                "args": {"message_id": "$.steps.draft.result.message_id"},
                "on_reject": "log_rejection",
            },
            next="done",
        ),
        step("done", "transform", {"set": {"outcome": "sent"}}),
        step("log_rejection", "transform", {"set": {"outcome": "not sent"}}),
    ],
    "output": {"outcome": "$.state.outcome"},
}

REFUND_WF = {
    "start": "refund",
    "inputs": {"order": {"type": "string"}, "cents": {"type": "integer"}},
    "steps": [
        step(
            "refund",
            "tool",
            {
                "tool": "payments.issue_refund",
                "args": {
                    "order_ref": "$.input.order",
                    "amount_cents": "$.input.cents",
                    "reason": "late delivery",
                },
            },
        )
    ],
    "output": {"refund": "$.steps.refund.result"},
}


async def setup(api: Api, client: AsyncClient, wf: dict[str, Any]) -> WorkflowSetup:
    s = await make_workflow(api, wf)
    assert (await client.post(s.url("/demo-data"), headers=s.owner.headers)).status_code == 200
    await client.put(
        s.url("/tools/email.send_message"),
        json={
            "config": {"from_address": "support@acme.example"},
            "credentials": {"api_key": "sk-test"},
        },
        headers=s.owner.headers,
    )
    return s


async def pending(client: AsyncClient, s: WorkflowSetup, who: Session) -> list[dict[str, Any]]:
    r = await client.get(s.url("/approvals"), params={"status": "pending"}, headers=who.headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def decide(
    client: AsyncClient, s: WorkflowSetup, who: Session, approval_id: str, **body: Any
) -> Any:
    return await client.post(
        s.url(f"/approvals/{approval_id}/decision"), json=body, headers=who.headers
    )


async def test_approve_resumes_and_executes_exactly_once(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor, db: AsyncSession
) -> None:
    s = await setup(api, client, EMAIL_WF)
    eid = await start(api, s, {"to": "ada@example.com"})
    await engine.run_until_idle()

    ex = await get_exec(api, s, eid)
    assert ex["status"] == "waiting" and ex["waiting_on"]["reason"] == "tool_approval"
    [req] = await pending(client, s, s.owner)
    assert req["tool_name"] == "email.send_message" and req["risk_level"] == "external_action"
    assert req["execution_id"] == eid and req["required_permission"] == "approval:decide"
    assert "message_id" in req["proposed_args"]
    assert (await db.scalar(sa.select(SimMessage.status))) == "draft"  # nothing sent yet
    assert await engine.claim() is None  # waiting holds no worker

    approver = await api.member(s.org, s.owner, "operator")
    r = await decide(client, s, approver, req["id"], decision="approve", comment="ok to send")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert (await get_exec(api, s, eid))["status"] == "queued"
    await engine.run_until_idle()

    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded" and ex["output"] == {"outcome": "sent"}
    assert (await db.scalar(sa.select(SimMessage.status))) == "sent"
    sends = (
        await db.scalars(
            sa.select(ToolCall).where(
                ToolCall.tool_name == "email.send_message", ToolCall.status == "succeeded"
            )
        )
    ).all()
    assert len(sends) == 1
    audit = (await db.scalars(sa.select(AuditEvent.event_type))).all()
    assert {"approval.requested", "approval.approved", "tool.executed"} <= set(audit)
    executed = (
        await db.scalars(
            sa.select(AuditEvent).where(
                AuditEvent.event_type == "tool.executed",
                AuditEvent.event_metadata["tool"].as_string() == "email.send_message",
            )
        )
    ).one()
    assert executed.event_metadata["approval_id"] == req["id"]  # action traceable to its approval


async def test_reject_routes_to_on_reject(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor, db: AsyncSession
) -> None:
    s = await setup(api, client, EMAIL_WF)
    eid = await start(api, s, {"to": "ada@example.com"})
    await engine.run_until_idle()
    [req] = await pending(client, s, s.owner)
    r = await decide(client, s, s.owner, req["id"], decision="reject", comment="tone is off")
    assert r.json()["status"] == "rejected"
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded" and ex["output"] == {"outcome": "not sent"}
    assert (await db.scalar(sa.select(SimMessage.status))) == "draft"


async def test_modified_arguments_are_validated_and_used(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor, db: AsyncSession
) -> None:
    s = await setup(api, client, REFUND_WF)
    initiator = await api.member(s.org, s.owner, "operator")
    eid = (
        await client.post(
            s.url(f"/workflows/{s.workflow}/executions"),
            json={"input": {"order": "O-50003", "cents": 500_000}},
            headers=initiator.headers,
        )
    ).json()["id"]
    await engine.run_until_idle()
    [req] = await pending(client, s, s.owner)
    assert req["required_permission"] == "approval:decide_high_risk"

    bad = await decide(
        client,
        s,
        s.owner,
        req["id"],
        decision="approve",
        args={"order_ref": "O-50003", "amount_cents": -5, "reason": "x"},
    )
    assert bad.status_code == 422
    smaller = {"order_ref": "O-50003", "amount_cents": 1000, "reason": "goodwill credit"}
    r = await decide(client, s, s.owner, req["id"], decision="approve", args=smaller)
    assert r.status_code == 200 and r.json()["approved_args"] == smaller
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded" and ex["output"]["refund"]["amount_cents"] == 1000
    assert await db.scalar(sa.select(sa.func.sum(SimRefund.amount_cents))) == 1000


async def test_decision_permissions_and_four_eyes(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor
) -> None:
    s = await setup(api, client, REFUND_WF)
    initiator = await api.member(s.org, s.owner, "admin")
    eid = (
        await client.post(
            s.url(f"/workflows/{s.workflow}/executions"),
            json={"input": {"order": "O-50004", "cents": 100}},
            headers=initiator.headers,
        )
    ).json()["id"]
    await engine.run_until_idle()
    [req] = await pending(client, s, s.owner)

    viewer = await api.member(s.org, s.owner, "viewer")
    operator = await api.member(s.org, s.owner, "operator")
    assert (await decide(client, s, viewer, req["id"], decision="approve")).status_code == 403
    r = await decide(client, s, operator, req["id"], decision="approve")
    assert (
        r.status_code == 403
        and r.json()["error"]["details"]["required_permission"] == "approval:decide_high_risk"
    )
    r = await decide(client, s, initiator, req["id"], decision="approve")  # admin, but requested it
    assert r.status_code == 403 and "Four-eyes" in r.json()["error"]["message"]
    assert (await decide(client, s, s.owner, req["id"], decision="approve")).status_code == 200
    assert (
        await decide(client, s, s.owner, req["id"], decision="reject")
    ).status_code == 409  # final
    await engine.run_until_idle()
    assert (await get_exec(api, s, eid))["status"] == "succeeded"


async def test_generic_resume_cannot_bypass_tool_approval(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor, db: AsyncSession
) -> None:
    s = await setup(api, client, REFUND_WF)
    eid = await start(api, s, {"order": "O-50005", "cents": 100})
    await engine.run_until_idle()
    r = await client.post(
        s.url(f"/executions/{eid}/resume"),
        json={"payload": {"approval_id": "x", "decision": "approved"}},
        headers=s.owner.headers,
    )
    assert r.status_code == 409
    await engine.run_until_idle()
    assert await db.scalar(sa.select(sa.func.count()).select_from(SimRefund)) == 0


async def test_policy_block_after_approval_still_wins(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor, db: AsyncSession
) -> None:
    s = await setup(api, client, EMAIL_WF)
    eid = await start(api, s, {"to": "ada@example.com"})
    await engine.run_until_idle()
    [req] = await pending(client, s, s.owner)
    await client.put(
        s.url("/tool-policy"),
        json={"blocked_tools": ["email.send_message"]},
        headers=s.owner.headers,
    )
    await decide(client, s, s.owner, req["id"], decision="approve")
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed" and ex["error"]["code"] == "tool_denied"
    assert (await db.scalar(sa.select(SimMessage.status))) == "draft"


async def test_expired_approvals_fail_the_step(
    api: Api,
    client: AsyncClient,
    app: FastAPI,
    engine: Engine,
    tool_harness: ToolExecutor,
    db: AsyncSession,
) -> None:
    # Same flow without an on_reject route (and without the step it pointed to).
    send = {
        **EMAIL_WF["steps"][1],
        "config": {k: v for k, v in EMAIL_WF["steps"][1]["config"].items() if k != "on_reject"},
    }
    wf = {**EMAIL_WF, "steps": [EMAIL_WF["steps"][0], send, EMAIL_WF["steps"][2]]}
    s = await setup(api, client, wf)
    eid = await start(api, s, {"to": "ada@example.com"})
    await engine.run_until_idle()
    await db.execute(sa.update(Approval).values(expires_at=utcnow() - timedelta(seconds=1)))
    await db.commit()
    [req] = await pending(client, s, s.owner)
    assert (await decide(client, s, s.owner, req["id"], decision="approve")).status_code == 409

    assert await expire_due(app.state.sessionmaker, app.state.step_registry) == 1
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed" and ex["error"]["code"] == "tool_approval_expired"
    assert (await db.scalar(sa.select(Approval.status))).value == "expired"


async def test_cancelling_execution_cancels_pending_approval(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor
) -> None:
    s = await setup(api, client, EMAIL_WF)
    eid = await start(api, s, {"to": "ada@example.com"})
    await engine.run_until_idle()
    [req] = await pending(client, s, s.owner)
    await client.post(s.url(f"/executions/{eid}/cancel"), headers=s.owner.headers)
    got = (await client.get(s.url(f"/approvals/{req['id']}"), headers=s.owner.headers)).json()
    assert got["status"] == "cancelled"
    assert (await decide(client, s, s.owner, req["id"], decision="approve")).status_code == 409


@pytest.mark.security
async def test_approvals_are_tenant_isolated(
    api: Api, client: AsyncClient, engine: Engine, tool_harness: ToolExecutor
) -> None:
    s = await setup(api, client, EMAIL_WF)
    await start(api, s, {"to": "ada@example.com"})
    await engine.run_until_idle()
    [req] = await pending(client, s, s.owner)
    attacker = await api.user()
    attacker_org = await api.org(attacker)
    for org in (s.org, attacker_org):
        base = f"/api/v1/orgs/{org}/approvals"
        assert (
            await client.get(f"{base}/{req['id']}", headers=attacker.headers)
        ).status_code == 404
        r = await client.post(
            f"{base}/{req['id']}/decision", json={"decision": "approve"}, headers=attacker.headers
        )
        assert r.status_code == 404
    assert (
        await client.get(f"/api/v1/orgs/{attacker_org}/approvals", headers=attacker.headers)
    ).json() == []
