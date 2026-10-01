"""Simulated external systems (CRM, order DB, ticketing, email, payments).

These are stand-ins for a customer's real systems, implemented as real tenant-scoped tables
behind the same tool interface a real connector would use. They give the three customer
scenarios realistic, queryable data and observable side effects without third-party
accounts. The ``sim_`` prefix keeps them clearly separate from platform tables.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from solutionforge.core.clock import utcnow
from solutionforge.db.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin


class SimCustomer(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "sim_customers"
    __table_args__ = (sa.UniqueConstraint("organization_id", "ref"),)

    ref: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    tier: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # standard|gold|platinum
    account_status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    lifetime_value_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class SimOrder(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "sim_orders"
    __table_args__ = (sa.UniqueConstraint("organization_id", "ref"),)

    ref: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("sim_customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # placed|shipped|delivered
    promised_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    delivered_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    delay_reason: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    total_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class SimTicket(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "sim_tickets"
    __table_args__ = (
        sa.UniqueConstraint("organization_id", "ref"),
        sa.UniqueConstraint("organization_id", "idempotency_key"),
    )

    ref: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("sim_customers.id", ondelete="SET NULL"), nullable=True
    )
    subject: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    priority: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="open")
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class SimMessage(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Email outbox. 'Sending' marks the row sent; no real email leaves the system."""

    __tablename__ = "sim_messages"
    __table_args__ = (sa.UniqueConstraint("organization_id", "idempotency_key"),)

    to_address: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    subject: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(8), nullable=False)  # draft|sent
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SimRefund(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "sim_refunds"
    __table_args__ = (sa.UniqueConstraint("organization_id", "idempotency_key"),)

    order_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("sim_orders.id", ondelete="CASCADE"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
