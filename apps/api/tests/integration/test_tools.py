"""ToolExecutor + simulated connectors + tool steps against a migrated database."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.domain import AuditEvent, SimMessage, SimTicket, ToolCall, ToolInstallation
from solutionforge.security.rbac import Role
from solutionforge.tools.executor import ToolExecutor
from solutionforge.tools.spec import (
    ToolApprovalRequired,
    ToolBusinessError,
    ToolInputInvalid,
    ToolNotEnabled,
    ToolOutputInvalid,
    ToolTimeout,
    ToolTransientError,
)
from solutionforge.workflows.engine import Engine
from tests.helpers import Api, Session
from tests.tool_support import CrashAfterTicket, FlakyRead, FlakyWriteNoKey
from tests.workflow_support import (
    SimulatedCrash,
    WorkflowSetup,
    get_exec,
    make_workflow,
    start,
    step,
)

TEST_TOOLS = [
    "test.flaky_read",
    "test.flaky_write",
    "test.slow",
    "test.boom",
    "test.bad_output",
    "test.crash_after_ticket",
]


async def seeded(
    api: Api, client: AsyncClient, owner: Session | None = None
) -> tuple[Session, uuid.UUID]:
    owner = owner or await api.user()
    org = await api.org(owner)
    r = await client.post(f"/api/v1/orgs/{org}/demo-data", headers=owner.headers)
    assert r.status_code == 200, r.text
    for name in TEST_TOOLS:
        r = await client.put(f"/api/v1/orgs/{org}/tools/{name}", json={}, headers=owner.headers)
        assert r.status_code == 200, r.text
    return owner, uuid.UUID(org)


@pytest.fixture
async def org(api: Api, client: AsyncClient, tool_harness: ToolExecutor) -> uuid.UUID:
    return (await seeded(api, client))[1]


# ------------------------------------------------------------------ seed + catalog


async def test_demo_seed_is_deterministic_and_idempotent(
    api: Api, client: AsyncClient, tool_harness: ToolExecutor
) -> None:
    owner = await api.user()
    org = await api.org(owner)
    first = (await client.post(f"/api/v1/orgs/{org}/demo-data", headers=owner.headers)).json()
    assert (first["customers"], first["orders"], first["skipped"]) == (24, 60, False)
    assert "crm.get_customer" in first["tools_installed"]
    again = (await client.post(f"/api/v1/orgs/{org}/demo-data", headers=owner.headers)).json()
    assert again["skipped"] is True and again["tools_installed"] == []

    tools = (await client.get(f"/api/v1/orgs/{org}/tools", headers=owner.headers)).json()
    by_name = {t["name"]: t for t in tools}
    assert by_name["payments.issue_refund"]["risk_level"] == "high_risk"
    assert by_name["crm.get_customer"]["mcp"]["annotations"]["readOnlyHint"] is True
    assert by_name["email.send_message"]["installation"]["has_credentials"] is False


# ------------------------------------------------------------------ executor semantics


async def test_read_tools_return_tenant_data_and_are_recorded(
    tool_harness: ToolExecutor, org: uuid.UUID, db: AsyncSession
) -> None:
    inv = await tool_harness.invoke(
        actor_role=Role.OPERATOR,
        organization_id=org,
        tool_name="crm.get_customer",
        args={"customer_ref": "C-1001"},
    )
    assert inv.output["ref"] == "C-1001" and inv.output["tier"] in {"standard", "gold", "platinum"}
    delayed = await tool_harness.invoke(
        actor_role=Role.OPERATOR,
        organization_id=org,
        tool_name="orders.find_delayed",
        args={"min_days_late": 3, "tiers": ["gold", "platinum"]},
    )
    orders = delayed.output["orders"]
    assert orders and all(
        o["days_late"] > 3 and o["customer_tier"] in {"gold", "platinum"} for o in orders
    )
    calls = (await db.scalars(sa.select(ToolCall).order_by(ToolCall.created_at))).all()
    assert [(c.tool_name, c.status) for c in calls] == [
        ("crm.get_customer", "succeeded"),
        ("orders.find_delayed", "succeeded"),
    ]


async def test_business_errors_are_not_retried(tool_harness: ToolExecutor, org: uuid.UUID) -> None:
    with pytest.raises(ToolBusinessError):
        await tool_harness.invoke(
            actor_role=Role.OPERATOR,
            organization_id=org,
            tool_name="crm.get_customer",
            args={"customer_ref": "C-9999"},
        )


async def test_uninstalled_or_disabled_tools_refused(
    api: Api, client: AsyncClient, tool_harness: ToolExecutor
) -> None:
    owner = await api.user()
    org = uuid.UUID(await api.org(owner))
    with pytest.raises(ToolNotEnabled):
        await tool_harness.invoke(
            actor_role=Role.OPERATOR, organization_id=org, tool_name="crm.search_customers", args={}
        )
    await client.put(
        f"/api/v1/orgs/{org}/tools/crm.search_customers",
        json={"enabled": False},
        headers=owner.headers,
    )
    with pytest.raises(ToolNotEnabled):
        await tool_harness.invoke(
            actor_role=Role.OPERATOR, organization_id=org, tool_name="crm.search_customers", args={}
        )


async def test_invalid_args_rejected_before_execution(
    tool_harness: ToolExecutor, org: uuid.UUID, db: AsyncSession
) -> None:
    with pytest.raises(ToolInputInvalid) as ei:
        await tool_harness.invoke(
            actor_role=Role.OPERATOR,
            organization_id=org,
            tool_name="ticketing.create_ticket",
            args={"subject": "x", "body": "y", "priority": "urgent", "assignee": "root"},
        )
    assert "root" not in str(ei.value.details)  # offending values are not echoed
    assert await db.scalar(sa.select(sa.func.count()).select_from(SimTicket)) == 0


async def test_transient_errors_retried_only_when_safe(
    tool_harness: ToolExecutor, org: uuid.UUID
) -> None:
    flaky_read = tool_harness.catalog.get("test.flaky_read")
    assert isinstance(flaky_read, FlakyRead)
    inv = await tool_harness.invoke(
        actor_role=Role.OPERATOR, organization_id=org, tool_name="test.flaky_read", args={}
    )
    assert inv.output == {"calls": 3} and inv.attempts == 3

    flaky_write = tool_harness.catalog.get("test.flaky_write")
    assert isinstance(flaky_write, FlakyWriteNoKey)
    with pytest.raises(ToolTransientError):
        await tool_harness.invoke(
            actor_role=Role.OPERATOR, organization_id=org, tool_name="test.flaky_write", args={}
        )
    assert flaky_write.calls == 1  # a non-deduplicating write is never blindly repeated


async def test_timeout_crash_and_bad_output_are_contained(
    tool_harness: ToolExecutor, org: uuid.UUID, db: AsyncSession
) -> None:
    with pytest.raises(ToolTimeout):
        await tool_harness.invoke(
            actor_role=Role.OPERATOR, organization_id=org, tool_name="test.slow", args={}
        )
    with pytest.raises(ToolTransientError) as ei:
        await tool_harness.invoke(
            actor_role=Role.OPERATOR, organization_id=org, tool_name="test.boom", args={}
        )
    assert "hunter2" not in str(ei.value) and "hunter2" not in str(ei.value.details)
    with pytest.raises(ToolOutputInvalid):
        await tool_harness.invoke(
            actor_role=Role.OPERATOR, organization_id=org, tool_name="test.bad_output", args={}
        )
    rows = (await db.scalars(sa.select(ToolCall.error))).all()
    assert "hunter2" not in str(rows)


# ------------------------------------------------------------------ idempotency


TICKET = {
    "customer_ref": "C-1002",
    "subject": "Order delayed",
    "body": "Investigate",
    "priority": "high",
}


async def test_same_key_creates_one_ticket_and_replays(
    tool_harness: ToolExecutor, org: uuid.UUID, db: AsyncSession
) -> None:
    a = await tool_harness.invoke(
        actor_role=Role.OPERATOR,
        organization_id=org,
        tool_name="ticketing.create_ticket",
        args=TICKET,
        idempotency_key="exec-1:create:1",
    )
    b = await tool_harness.invoke(
        actor_role=Role.OPERATOR,
        organization_id=org,
        tool_name="ticketing.create_ticket",
        args=TICKET,
        idempotency_key="exec-1:create:1",
    )
    c = await tool_harness.invoke(
        actor_role=Role.OPERATOR,
        organization_id=org,
        tool_name="ticketing.create_ticket",
        args=TICKET,
        idempotency_key="exec-1:create:2",
    )
    assert a.output["created"] is True and not a.replayed
    assert b.replayed and b.output == a.output and b.tool_call_id == a.tool_call_id
    assert c.output["ticket_ref"] != a.output["ticket_ref"]
    assert await db.scalar(sa.select(sa.func.count()).select_from(SimTicket)) == 2


async def test_concurrent_same_key_creates_exactly_one_ticket(
    tool_harness: ToolExecutor, org: uuid.UUID, db: AsyncSession
) -> None:
    results = await asyncio.gather(
        *[
            tool_harness.invoke(
                actor_role=Role.OPERATOR,
                organization_id=org,
                tool_name="ticketing.create_ticket",
                args=TICKET,
                idempotency_key="race-key-1",
            )
            for _ in range(5)
        ],
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    assert ok, results
    assert len({r.output["ticket_ref"] for r in ok}) == 1
    assert await db.scalar(sa.select(sa.func.count()).select_from(SimTicket)) == 1


# ------------------------------------------------------------------ policy gate


async def test_external_and_high_risk_tools_require_approval_and_are_audited(
    tool_harness: ToolExecutor, org: uuid.UUID, db: AsyncSession
) -> None:
    draft = await tool_harness.invoke(
        actor_role=Role.OPERATOR,
        organization_id=org,
        tool_name="email.draft_message",
        args={"to": "ada@example.com", "subject": "Your order", "body": "Sorry for the delay"},
    )
    with pytest.raises(ToolApprovalRequired):
        await tool_harness.invoke(
            actor_role=Role.OPERATOR,
            organization_id=org,
            tool_name="email.send_message",
            args={"message_id": draft.output["message_id"]},
        )
    with pytest.raises(ToolApprovalRequired) as ei:
        await tool_harness.invoke(
            actor_role=Role.OPERATOR,
            organization_id=org,
            tool_name="payments.issue_refund",
            args={"order_ref": "O-50001", "amount_cents": 100, "reason": "late"},
        )
    assert ei.value.details["approver_permission"] == "approval:decide_high_risk"

    msg = (await db.scalars(sa.select(SimMessage))).one()
    assert msg.status == "draft"  # nothing left the building
    denied = (await db.scalars(sa.select(ToolCall).where(ToolCall.status == "denied"))).all()
    assert {d.tool_name for d in denied} == {"email.send_message", "payments.issue_refund"}
    audits = (await db.scalars(sa.select(AuditEvent.event_type))).all()
    assert audits.count("tool.denied") == 2 and "tool.executed" in audits  # the draft


async def test_tenant_policy_can_require_approval_for_low_risk_writes(
    api: Api, client: AsyncClient, tool_harness: ToolExecutor
) -> None:
    owner, org = await seeded(api, client)
    await client.put(
        f"/api/v1/orgs/{org}/tools/ticketing.create_ticket",
        json={"auto_approve_low_risk": False},
        headers=owner.headers,
    )
    with pytest.raises(ToolApprovalRequired):
        await tool_harness.invoke(
            actor_role=Role.OPERATOR,
            organization_id=org,
            tool_name="ticketing.create_ticket",
            args=TICKET,
        )


# ------------------------------------------------------------------ credentials


async def test_credentials_are_write_only_encrypted_and_audited_without_values(
    api: Api, client: AsyncClient, tool_harness: ToolExecutor, db: AsyncSession
) -> None:
    owner, org = await seeded(api, client)
    url = f"/api/v1/orgs/{org}/tools/email.send_message"
    r = await client.put(
        url,
        json={
            "config": {"from_address": "support@acme.example"},
            "credentials": {"api_key": "sk-live-SECRET-123"},
        },
        headers=owner.headers,
    )
    assert r.status_code == 200
    assert r.json()["installation"]["has_credentials"] is True
    assert "SECRET" not in r.text
    listing = await client.get(f"/api/v1/orgs/{org}/tools", headers=owner.headers)
    assert "SECRET" not in listing.text

    inst = (
        await db.scalars(
            sa.select(ToolInstallation).where(
                ToolInstallation.organization_id == org,
                ToolInstallation.tool_name == "email.send_message",
            )
        )
    ).one()
    assert inst.credentials_encrypted is not None and b"SECRET" not in inst.credentials_encrypted
    audit = (
        await db.scalars(
            sa.select(AuditEvent).where(AuditEvent.event_type == "tool.credentials_updated")
        )
    ).one()
    assert audit.event_metadata == {"action": "set", "fields": ["api_key"]}

    # Omitting credentials keeps them; null clears them.
    await client.put(
        url, json={"config": {"from_address": "x@acme.example"}}, headers=owner.headers
    )
    await db.refresh(inst)
    assert inst.credentials_encrypted is not None
    r = await client.put(url, json={"credentials": None}, headers=owner.headers)
    assert r.json()["installation"]["has_credentials"] is False


async def test_installation_validation(
    api: Api, client: AsyncClient, tool_harness: ToolExecutor
) -> None:
    owner, org = await seeded(api, client)
    r = await client.put(
        f"/api/v1/orgs/{org}/tools/email.send_message",
        json={"config": {"smtp_host": "evil.example"}},
        headers=owner.headers,
    )
    assert r.status_code == 422
    r = await client.put(f"/api/v1/orgs/{org}/tools/shell.exec", json={}, headers=owner.headers)
    assert r.status_code == 404
    r = await client.put(f"/api/v1/orgs/{org}/tools/Not-A-Name", json={}, headers=owner.headers)
    assert r.status_code == 422


# ------------------------------------------------------------------ tools in workflows


def _wf(steps: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {"start": steps[0]["id"], "steps": steps, **kw}


async def _workflow(api: Api, client: AsyncClient, definition: dict[str, Any]) -> WorkflowSetup:
    s = await make_workflow(api, definition)
    r = await client.post(s.url("/demo-data"), headers=s.owner.headers)
    assert r.status_code == 200
    for name in TEST_TOOLS:
        await client.put(s.url(f"/tools/{name}"), json={}, headers=s.owner.headers)
    return s


async def test_support_flow_lookup_ticket_and_draft(
    api: Api, client: AsyncClient, engine: Engine
) -> None:
    definition = _wf(
        [
            step(
                "lookup",
                "tool",
                {"tool": "crm.get_customer", "args": {"customer_ref": "$.input.customer"}},
                next="ticket",
            ),
            step(
                "ticket",
                "tool",
                {
                    "tool": "ticketing.create_ticket",
                    "args": {
                        "customer_ref": "$.input.customer",
                        "subject": "Delayed order for {{ $.steps.lookup.result.name }}",
                        "body": "Customer tier: {{ $.steps.lookup.result.tier }}",
                        "priority": "high",
                    },
                },
                next="draft",
            ),
            step(
                "draft",
                "tool",
                {
                    "tool": "email.draft_message",
                    "args": {
                        "to": "$.steps.lookup.result.email",
                        "subject": "About ticket {{ $.steps.ticket.result.ticket_ref }}",
                        "body": "We are on it.",
                    },
                },
            ),
        ],
        inputs={"customer": {"type": "string"}},
        output={
            "ticket": "$.steps.ticket.result.ticket_ref",
            "draft": "$.steps.draft.result.status",
        },
    )
    s = await _workflow(api, client, definition)
    eid = await start(api, s, {"customer": "C-1003"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded", ex["error"]
    assert ex["output"]["draft"] == "draft" and ex["output"]["ticket"].startswith("T-")
    calls = (
        await client.get(
            s.url("/tool-calls"), params={"execution_id": eid}, headers=s.owner.headers
        )
    ).json()
    assert [c["tool_name"] for c in reversed(calls)] == [
        "crm.get_customer",
        "ticketing.create_ticket",
        "email.draft_message",
    ]
    assert all(c["idempotency_key"].startswith(eid) for c in calls)
    activity = (await client.get(s.url("/simulated/activity"), headers=s.owner.headers)).json()
    assert len(activity["tickets"]) == 1 and activity["messages"][0]["status"] == "draft"


async def test_sending_email_from_workflow_is_blocked_until_approval_exists(
    api: Api, client: AsyncClient, engine: Engine
) -> None:
    definition = _wf(
        [
            step(
                "draft",
                "tool",
                {
                    "tool": "email.draft_message",
                    "args": {"to": "a@example.com", "subject": "Hi", "body": "Hello"},
                },
                next="send",
            ),
            step(
                "send",
                "tool",
                {
                    "tool": "email.send_message",
                    "args": {"message_id": "$.steps.draft.result.message_id"},
                },
            ),
        ]
    )
    s = await _workflow(api, client, definition)
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed" and ex["error"]["code"] == "tool_approval_required"
    assert ex["error"]["retryable"] is False


async def test_crash_after_side_effect_does_not_duplicate_on_recovery(
    api: Api,
    client: AsyncClient,
    app: Any,
    engine: Engine,
    db: AsyncSession,
    tool_harness: ToolExecutor,
) -> None:
    definition = _wf(
        [
            step(
                "create",
                "tool",
                {"tool": "test.crash_after_ticket", "args": {"subject": "S", "body": "B"}},
                retry={"max_attempts": 3, "initial_backoff_seconds": 0},
            ),
        ]
    )
    s = await _workflow(api, client, definition)
    eid = await start(api, s)
    with pytest.raises(SimulatedCrash):  # ticket written, worker dies before checkpoint
        await engine.run_until_idle()
    await db.execute(sa.text("UPDATE executions SET lease_expires_at = '2000-01-01 00:00:00'"))
    await db.commit()
    recovered = Engine(app.state.sessionmaker, app.state.step_registry, worker_id="worker-2")
    await recovered.run_until_idle()

    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded"
    assert ex["steps"][-1]["output"]["result"]["created"] is False  # deduplicated on retry
    assert await db.scalar(sa.select(sa.func.count()).select_from(SimTicket)) == 1
    crash_tool = tool_harness.catalog.get("test.crash_after_ticket")
    assert isinstance(crash_tool, CrashAfterTicket) and sum(crash_tool.calls.values()) == 2


async def test_loop_revisiting_a_tool_step_gets_a_fresh_idempotency_key(
    api: Api, client: AsyncClient, engine: Engine, db: AsyncSession
) -> None:
    definition = _wf(
        [
            step(
                "create",
                "tool",
                {"tool": "ticketing.create_ticket", "args": {"subject": "Round", "body": "B"}},
                next="check",
            ),
            step(
                "check",
                "condition",
                {
                    "branches": [{"when": {"path": "$.state.done", "op": "exists"}, "goto": None}],
                    "default": "mark",
                },
            ),
            step("mark", "transform", {"set": {"done": True}}, next="create"),
        ]
    )
    s = await _workflow(api, client, definition)
    await start(api, s)
    await engine.run_until_idle()
    assert await db.scalar(sa.select(sa.func.count()).select_from(SimTicket)) == 2
    keys = (await db.scalars(sa.select(ToolCall.idempotency_key))).all()
    assert len(set(keys)) == 2


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"tool": "shell.exec", "args": {}}, "unknown tool"),
        (
            {"tool": "crm.get_customer", "args": {"customer_ref": "C-1", "sql": "x"}},
            "unknown argument",
        ),
        ({"tool": "ticketing.create_ticket", "args": {"subject": "x"}}, "missing required"),
        ({"tool": "crm.get_customer", "args": {"customer_ref": "$.steps.nope.x"}}, "unknown step"),
    ],
)
async def test_tool_step_config_validated_at_version_creation(
    api: Api, client: AsyncClient, tool_harness: ToolExecutor, config: dict[str, Any], needle: str
) -> None:
    s = await make_workflow(api, _wf([step("a", "transform")]))
    r = await client.post(
        s.url(f"/workflows/{s.workflow}/versions"),
        json={"definition": _wf([step("t", "tool", config)])},
        headers=s.owner.headers,
    )
    assert r.status_code == 422 and needle in str(r.json()["error"]["details"])
