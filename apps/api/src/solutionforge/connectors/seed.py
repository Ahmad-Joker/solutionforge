"""Deterministic demo data for the simulated systems (same seed + date → same data)."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.domain.simulated import SimCustomer, SimOrder

_FIRST = [
    "Ada",
    "Grace",
    "Alan",
    "Linus",
    "Margaret",
    "Ken",
    "Barbara",
    "Edsger",
    "Frances",
    "Donald",
    "Radia",
    "Tim",
    "Hedy",
    "Dennis",
    "Katherine",
    "John",
]
_LAST = [
    "Lovelace",
    "Hopper",
    "Turing",
    "Torvalds",
    "Hamilton",
    "Thompson",
    "Liskov",
    "Dijkstra",
    "Allen",
    "Knuth",
    "Perlman",
    "Berners",
    "Lamarr",
    "Ritchie",
    "Johnson",
    "Backus",
]
_DELAY_REASONS = [
    "carrier capacity shortage",
    "item backordered at warehouse",
    "customs inspection",
    "address verification failed",
    "weather disruption at hub",
]


@dataclass(frozen=True, slots=True)
class SeedResult:
    customers: int
    orders: int
    skipped: bool


async def seed_simulated_systems(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    today: date,
    seed: int = 7,
    customers: int = 24,
    orders: int = 60,
) -> SeedResult:
    """Idempotent: does nothing if the organization already has simulated customers."""
    exists = await session.scalar(
        sa.select(SimCustomer.id).where(SimCustomer.organization_id == organization_id).limit(1)
    )
    if exists is not None:
        return SeedResult(0, 0, skipped=True)

    rng = random.Random(seed)  # noqa: S311 - deterministic demo data, not security
    made: list[SimCustomer] = []
    for i in range(customers):
        first, last = rng.choice(_FIRST), rng.choice(_LAST)
        tier = rng.choices(["standard", "gold", "platinum"], weights=[5, 3, 2])[0]
        c = SimCustomer(
            organization_id=organization_id,
            ref=f"C-{1001 + i}",
            name=f"{first} {last}",
            email=f"{first}.{last}{i}@example.com".lower(),
            tier=tier,
            account_status=rng.choices(["active", "active", "active", "past_due"])[0],
            lifetime_value_cents={"standard": 1, "gold": 5, "platinum": 20}[tier]
            * rng.randint(10_000, 90_000),
        )
        session.add(c)
        made.append(c)
    await session.flush()

    for j in range(orders):
        promised = today + timedelta(days=rng.randint(-20, 10))
        delivered: date | None = None
        reason: str | None = None
        if promised >= today:
            status = rng.choice(["placed", "shipped"])
        else:
            roll = rng.random()
            if roll < 0.55:
                status, delivered = "delivered", promised - timedelta(days=rng.randint(0, 2))
            elif roll < 0.85:
                status, reason = "shipped", rng.choice(_DELAY_REASONS)  # still not delivered
            else:
                status = "delivered"
                delivered = min(today, promised + timedelta(days=rng.randint(1, 6)))
                reason = rng.choice(_DELAY_REASONS)
        session.add(
            SimOrder(
                organization_id=organization_id,
                ref=f"O-{50001 + j}",
                customer_id=rng.choice(made).id,
                status=status,
                promised_date=promised,
                delivered_date=delivered,
                delay_reason=reason,
                total_cents=rng.randint(1_500, 250_000),
            )
        )
    await session.flush()
    return SeedResult(customers, orders, skipped=False)
