"""Simulated connectors: CRM, orders, ticketing, email, payments.

Each tool behaves like a well-designed external API: typed inputs with length bounds,
tenant scoping on every query, business-rule errors, and idempotency keys on writes (a
repeated call with the same key returns the original result instead of acting twice).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Annotated, Literal

import sqlalchemy as sa
from pydantic import EmailStr, Field, StringConstraints, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.domain.simulated import SimCustomer, SimMessage, SimOrder, SimRefund, SimTicket
from solutionforge.security.rbac import Permission
from solutionforge.tools.spec import (
    RiskLevel,
    Tool,
    ToolBusinessError,
    ToolConfigError,
    ToolContext,
    ToolModel,
    ToolSpec,
)

CustomerRef = Annotated[str, StringConstraints(pattern=r"^C-\d{4,8}$")]
OrderRef = Annotated[str, StringConstraints(pattern=r"^O-\d{4,8}$")]
TicketRef = Annotated[str, StringConstraints(pattern=r"^T-[0-9A-F]{8}$")]
# Single line: no CR/LF, which blocks header injection in email subjects.
OneLine = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")]
Body = Annotated[str, StringConstraints(min_length=1, max_length=5000)]
Tier = Literal["standard", "gold", "platinum"]


# --------------------------------------------------------------------------- shared models


class CustomerOut(ToolModel):
    ref: str
    name: str
    email: str
    tier: Tier
    account_status: str
    lifetime_value_cents: int


class OrderOut(ToolModel):
    ref: str
    customer_ref: str
    customer_tier: Tier
    status: str
    promised_date: date
    delivered_date: date | None
    days_late: int
    delay_reason: str | None
    total_cents: int


def _customer_out(c: SimCustomer) -> CustomerOut:
    return CustomerOut(
        ref=c.ref,
        name=c.name,
        email=c.email,
        tier=c.tier,
        account_status=c.account_status,
        lifetime_value_cents=c.lifetime_value_cents,
    )


def days_late(o: SimOrder, as_of: date) -> int:
    end = o.delivered_date or as_of
    return max(0, (end - o.promised_date).days)


def _order_out(o: SimOrder, c: SimCustomer, as_of: date) -> OrderOut:
    return OrderOut(
        ref=o.ref,
        customer_ref=c.ref,
        customer_tier=c.tier,
        status=o.status,
        promised_date=o.promised_date,
        delivered_date=o.delivered_date,
        days_late=days_late(o, as_of),
        delay_reason=o.delay_reason,
        total_cents=o.total_cents,
    )


async def _customer_by_ref(s: AsyncSession, org: uuid.UUID, ref: str) -> SimCustomer:
    c = await s.scalar(
        sa.select(SimCustomer).where(SimCustomer.organization_id == org, SimCustomer.ref == ref)
    )
    if c is None:
        raise ToolBusinessError(f"customer {ref} not found")
    return c


# --------------------------------------------------------------------------- CRM


class GetCustomerIn(ToolModel):
    customer_ref: CustomerRef | None = None
    email: EmailStr | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> GetCustomerIn:
        if (self.customer_ref is None) == (self.email is None):
            raise ValueError("provide exactly one of customer_ref or email")
        return self


class GetCustomer(Tool):
    spec = ToolSpec(
        name="crm.get_customer",
        description="Look up one customer by reference (C-1234) or email.",
        input_model=GetCustomerIn,
        output_model=CustomerOut,
        risk_level=RiskLevel.READ_ONLY,
        required_permission=Permission.WORKFLOW_EXECUTE,
        max_attempts=3,
        idempotent=True,
    )

    async def execute(self, args: GetCustomerIn, ctx: ToolContext) -> CustomerOut:
        async with ctx.session() as s:
            if args.customer_ref is not None:
                return _customer_out(
                    await _customer_by_ref(s, ctx.organization_id, args.customer_ref)
                )
            c = await s.scalar(
                sa.select(SimCustomer).where(
                    SimCustomer.organization_id == ctx.organization_id,
                    SimCustomer.email == str(args.email).lower(),
                )
            )
            if c is None:
                raise ToolBusinessError("customer not found")
            return _customer_out(c)


class SearchCustomersIn(ToolModel):
    tier: Tier | None = None
    limit: int = Field(default=20, ge=1, le=50)


class SearchCustomersOut(ToolModel):
    customers: list[CustomerOut]


class SearchCustomers(Tool):
    spec = ToolSpec(
        name="crm.search_customers",
        description="List customers, optionally filtered by tier.",
        input_model=SearchCustomersIn,
        output_model=SearchCustomersOut,
        risk_level=RiskLevel.READ_ONLY,
        required_permission=Permission.WORKFLOW_EXECUTE,
        max_attempts=3,
        idempotent=True,
    )

    async def execute(self, args: SearchCustomersIn, ctx: ToolContext) -> SearchCustomersOut:
        stmt = sa.select(SimCustomer).where(SimCustomer.organization_id == ctx.organization_id)
        if args.tier is not None:
            stmt = stmt.where(SimCustomer.tier == args.tier)
        async with ctx.session() as s:
            rows = (await s.scalars(stmt.order_by(SimCustomer.ref).limit(args.limit))).all()
        return SearchCustomersOut(customers=[_customer_out(c) for c in rows])


# --------------------------------------------------------------------------- orders


class GetOrderIn(ToolModel):
    order_ref: OrderRef


class GetOrder(Tool):
    spec = ToolSpec(
        name="orders.get_order",
        description="Fetch one order with its delay status.",
        input_model=GetOrderIn,
        output_model=OrderOut,
        risk_level=RiskLevel.READ_ONLY,
        required_permission=Permission.WORKFLOW_EXECUTE,
        max_attempts=3,
        idempotent=True,
    )

    async def execute(self, args: GetOrderIn, ctx: ToolContext) -> OrderOut:
        async with ctx.session() as s:
            row = (
                await s.execute(
                    sa.select(SimOrder, SimCustomer)
                    .join(SimCustomer, SimCustomer.id == SimOrder.customer_id)
                    .where(
                        SimOrder.organization_id == ctx.organization_id,
                        SimOrder.ref == args.order_ref,
                    )
                )
            ).first()
        if row is None:
            raise ToolBusinessError(f"order {args.order_ref} not found")
        return _order_out(row[0], row[1], utcnow().date())


class FindDelayedIn(ToolModel):
    min_days_late: int = Field(default=3, ge=0, le=365)
    tiers: list[Tier] | None = Field(default=None, max_length=3)
    as_of: date | None = Field(default=None, description="Defaults to today (UTC)")
    limit: int = Field(default=50, ge=1, le=200)


class FindDelayedOut(ToolModel):
    as_of: date
    orders: list[OrderOut]


class FindDelayedOrders(Tool):
    spec = ToolSpec(
        name="orders.find_delayed",
        description="Orders more than N days late (undelivered past promise, or delivered late).",
        input_model=FindDelayedIn,
        output_model=FindDelayedOut,
        risk_level=RiskLevel.READ_ONLY,
        required_permission=Permission.WORKFLOW_EXECUTE,
        max_attempts=3,
        idempotent=True,
    )

    async def execute(self, args: FindDelayedIn, ctx: ToolContext) -> FindDelayedOut:
        as_of = args.as_of or utcnow().date()
        cutoff = as_of - timedelta(days=args.min_days_late)
        stmt = (
            sa.select(SimOrder, SimCustomer)
            .join(SimCustomer, SimCustomer.id == SimOrder.customer_id)
            .where(
                SimOrder.organization_id == ctx.organization_id,
                # Necessary condition for days_late > N, using only a portable date
                # comparison; the exact rule is applied below in Python.
                SimOrder.promised_date < cutoff,
            )
            .order_by(SimOrder.promised_date, SimOrder.ref)
        )
        if args.tiers:
            stmt = stmt.where(SimCustomer.tier.in_(args.tiers))
        async with ctx.session() as s:
            rows = (await s.execute(stmt)).all()
        # Exact lateness in Python: SQL date arithmetic is not portable across dialects.
        out = [_order_out(o, c, as_of) for o, c in rows if days_late(o, as_of) > args.min_days_late]
        return FindDelayedOut(as_of=as_of, orders=out[: args.limit])


# --------------------------------------------------------------------------- ticketing


class CreateTicketIn(ToolModel):
    customer_ref: CustomerRef | None = None
    subject: OneLine
    body: Body
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


class TicketOut(ToolModel):
    ticket_ref: str
    status: str
    priority: str
    created: bool


class CreateTicket(Tool):
    spec = ToolSpec(
        name="ticketing.create_ticket",
        description="Open a support ticket. Deduplicated by idempotency key.",
        input_model=CreateTicketIn,
        output_model=TicketOut,
        risk_level=RiskLevel.LOW_RISK_WRITE,
        required_permission=Permission.WORKFLOW_EXECUTE,
        max_attempts=3,
        supports_idempotency_key=True,
    )

    async def execute(self, args: CreateTicketIn, ctx: ToolContext) -> TicketOut:
        org = ctx.organization_id
        async with ctx.session() as s:
            if ctx.idempotency_key:
                existing = await s.scalar(
                    sa.select(SimTicket).where(
                        SimTicket.organization_id == org,
                        SimTicket.idempotency_key == ctx.idempotency_key,
                    )
                )
                if existing is not None:
                    return TicketOut(
                        ticket_ref=existing.ref,
                        status=existing.status,
                        priority=existing.priority,
                        created=False,
                    )
            customer_id = (
                (await _customer_by_ref(s, org, args.customer_ref)).id
                if args.customer_ref
                else None
            )
            ticket = SimTicket(
                organization_id=org,
                ref=f"T-{uuid.uuid4().hex[:8].upper()}",
                customer_id=customer_id,
                subject=args.subject,
                body=args.body,
                priority=args.priority,
                status="open",
                idempotency_key=ctx.idempotency_key,
            )
            s.add(ticket)
            try:
                await s.commit()
            except IntegrityError:  # concurrent duplicate with the same key won the race
                await s.rollback()
                winner = await s.scalar(
                    sa.select(SimTicket).where(
                        SimTicket.organization_id == org,
                        SimTicket.idempotency_key == ctx.idempotency_key,
                    )
                )
                if winner is None:
                    raise
                return TicketOut(
                    ticket_ref=winner.ref,
                    status=winner.status,
                    priority=winner.priority,
                    created=False,
                )
            return TicketOut(
                ticket_ref=ticket.ref, status="open", priority=args.priority, created=True
            )


class GetTicketIn(ToolModel):
    ticket_ref: TicketRef


class GetTicket(Tool):
    spec = ToolSpec(
        name="ticketing.get_ticket",
        description="Fetch a ticket's status.",
        input_model=GetTicketIn,
        output_model=TicketOut,
        risk_level=RiskLevel.READ_ONLY,
        required_permission=Permission.WORKFLOW_EXECUTE,
        max_attempts=3,
        idempotent=True,
    )

    async def execute(self, args: GetTicketIn, ctx: ToolContext) -> TicketOut:
        async with ctx.session() as s:
            t = await s.scalar(
                sa.select(SimTicket).where(
                    SimTicket.organization_id == ctx.organization_id,
                    SimTicket.ref == args.ticket_ref,
                )
            )
        if t is None:
            raise ToolBusinessError(f"ticket {args.ticket_ref} not found")
        return TicketOut(ticket_ref=t.ref, status=t.status, priority=t.priority, created=False)


# --------------------------------------------------------------------------- email


class DraftMessageIn(ToolModel):
    to: EmailStr
    subject: OneLine
    body: Body


class MessageOut(ToolModel):
    message_id: uuid.UUID
    to: str
    status: Literal["draft", "sent"]


class DraftMessage(Tool):
    spec = ToolSpec(
        name="email.draft_message",
        description="Save an email draft in the outbox (nothing is sent).",
        input_model=DraftMessageIn,
        output_model=MessageOut,
        risk_level=RiskLevel.LOW_RISK_WRITE,
        required_permission=Permission.WORKFLOW_EXECUTE,
        supports_idempotency_key=True,
        max_attempts=2,
    )

    async def execute(self, args: DraftMessageIn, ctx: ToolContext) -> MessageOut:
        async with ctx.session() as s:
            if ctx.idempotency_key:
                existing = await s.scalar(
                    sa.select(SimMessage).where(
                        SimMessage.organization_id == ctx.organization_id,
                        SimMessage.idempotency_key == ctx.idempotency_key,
                    )
                )
                if existing is not None:
                    return MessageOut(
                        message_id=existing.id, to=existing.to_address, status=existing.status
                    )
            msg = SimMessage(
                organization_id=ctx.organization_id,
                to_address=str(args.to).lower(),
                subject=args.subject,
                body=args.body,
                status="draft",
                idempotency_key=ctx.idempotency_key,
            )
            s.add(msg)
            await s.commit()
            return MessageOut(message_id=msg.id, to=msg.to_address, status="draft")


class SendMessageIn(ToolModel):
    message_id: uuid.UUID


class SendMessage(Tool):
    spec = ToolSpec(
        name="email.send_message",
        description="Send a drafted email to its recipient (external action).",
        input_model=SendMessageIn,
        output_model=MessageOut,
        risk_level=RiskLevel.EXTERNAL_ACTION,
        required_permission=Permission.WORKFLOW_EXECUTE,
        supports_idempotency_key=True,
        requires_credentials=True,
        config_keys=("from_address",),
    )

    async def execute(self, args: SendMessageIn, ctx: ToolContext) -> MessageOut:
        if not (ctx.credentials or {}).get("api_key"):
            raise ToolConfigError("email provider api_key is not configured")
        if not ctx.config.get("from_address"):
            raise ToolConfigError("email from_address is not configured")
        async with ctx.session() as s:
            msg = await s.scalar(
                sa.select(SimMessage)
                .where(
                    SimMessage.organization_id == ctx.organization_id,
                    SimMessage.id == args.message_id,
                )
                .with_for_update()
            )
            if msg is None:
                raise ToolBusinessError("message not found")
            if msg.status != "sent":  # sending twice is a no-op, not a second email
                msg.status = "sent"
                msg.sent_at = utcnow()
                await s.commit()
            return MessageOut(message_id=msg.id, to=msg.to_address, status="sent")


# --------------------------------------------------------------------------- payments


class IssueRefundIn(ToolModel):
    order_ref: OrderRef
    amount_cents: int = Field(gt=0, le=1_000_000)
    reason: OneLine


class RefundOut(ToolModel):
    refund_id: uuid.UUID
    order_ref: str
    amount_cents: int
    remaining_refundable_cents: int
    created: bool


class IssueRefund(Tool):
    spec = ToolSpec(
        name="payments.issue_refund",
        description="Refund part or all of an order (moves money; irreversible).",
        input_model=IssueRefundIn,
        output_model=RefundOut,
        risk_level=RiskLevel.HIGH_RISK,
        required_permission=Permission.WORKFLOW_EXECUTE,
        supports_idempotency_key=True,
    )

    async def execute(self, args: IssueRefundIn, ctx: ToolContext) -> RefundOut:
        org = ctx.organization_id
        async with ctx.session() as s:
            order = await s.scalar(
                sa.select(SimOrder)
                .where(SimOrder.organization_id == org, SimOrder.ref == args.order_ref)
                .with_for_update()
            )
            if order is None:
                raise ToolBusinessError(f"order {args.order_ref} not found")
            refunded = (
                await s.scalar(
                    sa.select(sa.func.coalesce(sa.func.sum(SimRefund.amount_cents), 0)).where(
                        SimRefund.organization_id == org, SimRefund.order_id == order.id
                    )
                )
                or 0
            )
            if ctx.idempotency_key:
                prior = await s.scalar(
                    sa.select(SimRefund).where(
                        SimRefund.organization_id == org,
                        SimRefund.idempotency_key == ctx.idempotency_key,
                    )
                )
                if prior is not None:
                    return RefundOut(
                        refund_id=prior.id,
                        order_ref=order.ref,
                        amount_cents=prior.amount_cents,
                        remaining_refundable_cents=order.total_cents - refunded,
                        created=False,
                    )
            if args.amount_cents > order.total_cents - refunded:
                raise ToolBusinessError(
                    "refund exceeds refundable amount",
                    refundable_cents=order.total_cents - refunded,
                )
            refund = SimRefund(
                organization_id=org,
                order_id=order.id,
                amount_cents=args.amount_cents,
                reason=args.reason,
                idempotency_key=ctx.idempotency_key,
            )
            s.add(refund)
            await s.commit()
            return RefundOut(
                refund_id=refund.id,
                order_ref=order.ref,
                amount_cents=args.amount_cents,
                remaining_refundable_cents=order.total_cents - refunded - args.amount_cents,
                created=True,
            )


SIMULATED_TOOLS: list[Tool] = [
    GetCustomer(),
    SearchCustomers(),
    GetOrder(),
    FindDelayedOrders(),
    CreateTicket(),
    GetTicket(),
    DraftMessage(),
    SendMessage(),
    IssueRefund(),
]
