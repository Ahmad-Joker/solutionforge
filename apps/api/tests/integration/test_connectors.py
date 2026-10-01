"""Connector behaviour tested directly (the executor's policy gate blocks the external and
high-risk ones until the approval flow exists, but their business rules must hold now)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.connectors import simulated as sim
from solutionforge.domain import SimMessage, SimOrder, SimRefund
from solutionforge.tools.spec import ToolBusinessError, ToolConfigError, ToolContext
from tests.helpers import Api


@pytest.fixture
async def org(api: Api, client: AsyncClient) -> uuid.UUID:
    owner = await api.user()
    org_id = await api.org(owner)
    await client.post(f"/api/v1/orgs/{org_id}/demo-data", headers=owner.headers)
    return uuid.UUID(org_id)


def ctx(app: FastAPI, org: uuid.UUID, key: str | None = None, **kw: Any) -> ToolContext:
    return ToolContext(
        organization_id=org,
        execution_id=None,
        step_id=None,
        idempotency_key=key,
        config=kw.get("config", {}),
        credentials=kw.get("credentials"),
        session=app.state.sessionmaker,
    )


async def test_read_tools(app: FastAPI, org: uuid.UUID) -> None:
    c = ctx(app, org)
    by_ref = await sim.GetCustomer().execute(sim.GetCustomerIn(customer_ref="C-1004"), c)
    by_email = await sim.GetCustomer().execute(sim.GetCustomerIn(email=by_ref.email.upper()), c)
    assert by_email.ref == "C-1004"  # emails match case-insensitively
    gold = await sim.SearchCustomers().execute(sim.SearchCustomersIn(tier="gold", limit=50), c)
    assert gold.customers and all(x.tier == "gold" for x in gold.customers)
    order = await sim.GetOrder().execute(sim.GetOrderIn(order_ref="O-50001"), c)
    assert order.ref == "O-50001" and order.days_late >= 0
    with pytest.raises(ToolBusinessError):
        await sim.GetOrder().execute(sim.GetOrderIn(order_ref="O-99999"), c)
    t = await sim.CreateTicket().execute(sim.CreateTicketIn(subject="s", body="b"), c)
    got = await sim.GetTicket().execute(sim.GetTicketIn(ticket_ref=t.ticket_ref), c)
    assert got.ticket_ref == t.ticket_ref and got.status == "open"


async def test_find_delayed_semantics(app: FastAPI, org: uuid.UUID, db: AsyncSession) -> None:
    out = await sim.FindDelayedOrders().execute(
        sim.FindDelayedIn(min_days_late=0, limit=200), ctx(app, org)
    )
    orders = (await db.scalars(sa.select(SimOrder).where(SimOrder.organization_id == org))).all()
    expected = {o.ref for o in orders if sim.days_late(o, out.as_of) > 0}
    assert {o.ref for o in out.orders} == expected  # SQL pre-filter agrees with the exact rule


async def test_send_requires_credentials_and_config_and_sends_once(
    app: FastAPI, org: uuid.UUID, db: AsyncSession
) -> None:
    draft = await sim.DraftMessage().execute(
        sim.DraftMessageIn(to="ada@example.com", subject="Hi", body="Hello"), ctx(app, org)
    )
    args = sim.SendMessageIn(message_id=draft.message_id)
    with pytest.raises(ToolConfigError, match="api_key"):
        await sim.SendMessage().execute(args, ctx(app, org))
    with pytest.raises(ToolConfigError, match="from_address"):
        await sim.SendMessage().execute(args, ctx(app, org, credentials={"api_key": "k"}))
    ok = ctx(app, org, credentials={"api_key": "k"}, config={"from_address": "s@acme.example"})
    first = await sim.SendMessage().execute(args, ok)
    msg = (await db.scalars(sa.select(SimMessage))).one()
    sent_at = msg.sent_at
    second = await sim.SendMessage().execute(args, ok)
    await db.refresh(msg)
    assert first.status == second.status == "sent" and msg.sent_at == sent_at  # not re-sent
    with pytest.raises(ToolBusinessError):
        await sim.SendMessage().execute(sim.SendMessageIn(message_id=uuid.uuid4()), ok)


async def test_refunds_enforce_refundable_amount_and_idempotency(
    app: FastAPI, org: uuid.UUID, db: AsyncSession
) -> None:
    order = (await db.scalars(sa.select(SimOrder).where(SimOrder.ref == "O-50002"))).one()
    tool = sim.IssueRefund()

    def req(amount: int) -> sim.IssueRefundIn:
        return sim.IssueRefundIn(order_ref="O-50002", amount_cents=amount, reason="late delivery")

    half = order.total_cents // 2
    r1 = await tool.execute(req(half), ctx(app, org, key="refund-1"))
    assert r1.created and r1.remaining_refundable_cents == order.total_cents - half
    replay = await tool.execute(req(half), ctx(app, org, key="refund-1"))
    assert not replay.created and replay.refund_id == r1.refund_id
    with pytest.raises(ToolBusinessError, match="exceeds"):
        await tool.execute(req(order.total_cents), ctx(app, org, key="refund-2"))
    with pytest.raises(ToolBusinessError, match="not found"):
        await tool.execute(
            sim.IssueRefundIn(order_ref="O-99999", amount_cents=1, reason="x"), ctx(app, org)
        )
    assert await db.scalar(sa.select(sa.func.sum(SimRefund.amount_cents))) == half
