"""Races, expiry, and pagination: the paths that break in production, not in demos."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.domain import RefreshToken
from tests.helpers import Api


async def test_concurrent_refresh_with_same_token_yields_one_winner(
    api: Api, client: AsyncClient
) -> None:
    s = await api.user()
    results = await asyncio.gather(
        *[
            client.post("/api/v1/auth/refresh", json={"refresh_token": s.refresh_token})
            for _ in range(5)
        ]
    )
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1, codes
    assert all(c == 401 for c in codes if c != 200), codes  # never a 500


async def test_concurrent_duplicate_registration(api: Api) -> None:
    results = await asyncio.gather(*[api.register("race@example.com") for _ in range(5)])
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1, codes
    assert all(c == 409 for c in codes if c != 201), codes


async def test_expired_refresh_token_rejected(
    api: Api, client: AsyncClient, db: AsyncSession
) -> None:
    s = await api.user()
    await db.execute(sa.update(RefreshToken).values(expires_at=utcnow() - timedelta(seconds=1)))
    await db.commit()
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": s.refresh_token})
    assert r.status_code == 401


async def test_audit_pagination_cursor(api: Api, client: AsyncClient) -> None:
    owner = await api.user()
    org = await api.org(owner)
    for i in range(4):
        await client.patch(f"/api/v1/orgs/{org}", json={"name": f"Name {i}"}, headers=owner.headers)

    url = f"/api/v1/orgs/{org}/audit-events"
    page1 = (await client.get(url, params={"limit": 3}, headers=owner.headers)).json()
    assert len(page1) == 3
    page2 = (
        await client.get(
            url, params={"limit": 3, "before": page1[-1]["created_at"]}, headers=owner.headers
        )
    ).json()
    assert page2
    assert not {e["id"] for e in page1} & {e["id"] for e in page2}
    assert all(e["created_at"] < page1[-1]["created_at"] for e in page2)


async def test_audit_limit_is_bounded(api: Api, client: AsyncClient) -> None:
    owner = await api.user()
    org = await api.org(owner)
    r = await client.get(
        f"/api/v1/orgs/{org}/audit-events", params={"limit": 10_000}, headers=owner.headers
    )
    assert r.status_code == 422


async def test_malformed_ids_are_validation_errors_not_500s(api: Api, client: AsyncClient) -> None:
    owner = await api.user()
    assert (await client.get("/api/v1/orgs/not-a-uuid", headers=owner.headers)).status_code == 422
    org = await api.org(owner)
    r = await client.delete(f"/api/v1/orgs/{org}/members/123", headers=owner.headers)
    assert r.status_code == 422


async def test_oversized_display_name_rejected(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "long@example.com",
            "password": "a-perfectly-fine-password",
            "display_name": "x" * 500,
        },
    )
    assert r.status_code == 422
