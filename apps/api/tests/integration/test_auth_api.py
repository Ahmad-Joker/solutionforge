from __future__ import annotations

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.domain import AuditEvent, RefreshToken, User
from tests.helpers import DEFAULT_PASSWORD, Api


async def test_register_login_me(api: Api, client: AsyncClient) -> None:
    r = await api.register("Alice@Example.com")
    assert r.status_code == 201
    assert r.json()["email"] == "alice@example.com"  # normalized
    assert "password" not in r.text and "password_hash" not in r.text

    r = await api.login("ALICE@example.com")
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


async def test_duplicate_email_is_case_insensitive(api: Api) -> None:
    assert (await api.register("bob@example.com")).status_code == 201
    r = await api.register("BOB@example.com")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


async def test_weak_password_rejected_without_echoing_it(api: Api) -> None:
    r = await api.register("carol@example.com", password="Zq9xPw!")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"
    assert "Zq9xPw!" not in r.text  # rejected input is never echoed back


async def test_login_failures_are_indistinguishable(api: Api) -> None:
    await api.register("dave@example.com")
    wrong_pw = await api.login("dave@example.com", "not-the-password")
    no_user = await api.login("nobody@example.com", "not-the-password")
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json()["error"]["message"] == no_user.json()["error"]["message"]
    assert wrong_pw.headers["www-authenticate"] == "Bearer"


async def test_failed_login_is_audited_without_password(api: Api, db: AsyncSession) -> None:
    await api.register("erin@example.com")
    await api.login("erin@example.com", "guess-number-one")
    ev = (
        await db.scalars(sa.select(AuditEvent).where(AuditEvent.event_type == "auth.login_failed"))
    ).one()
    assert ev.actor_user_id is not None
    assert "guess-number-one" not in str(ev.event_metadata)


async def test_inactive_user_cannot_login_or_use_tokens(
    api: Api, client: AsyncClient, db: AsyncSession
) -> None:
    s = await api.user("frank@example.com")
    await db.execute(sa.update(User).where(User.email == s.email).values(is_active=False))
    await db.commit()
    assert (await api.login(s.email)).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=s.headers)).status_code == 401
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": s.refresh_token})
    assert r.status_code == 401


async def test_refresh_rotates_tokens(api: Api, client: AsyncClient) -> None:
    s = await api.user()
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": s.refresh_token})
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != s.refresh_token
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"}
    )
    assert me.status_code == 200


async def test_refresh_token_reuse_revokes_whole_family(
    api: Api, client: AsyncClient, db: AsyncSession
) -> None:
    s = await api.user()
    first = await client.post("/api/v1/auth/refresh", json={"refresh_token": s.refresh_token})
    rotated = first.json()["refresh_token"]

    # Attacker replays the original (already rotated) token.
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": s.refresh_token})
    assert replay.status_code == 401

    # The legitimate client's current token is now dead too.
    legit = await client.post("/api/v1/auth/refresh", json={"refresh_token": rotated})
    assert legit.status_code == 401

    live = await db.scalar(
        sa.select(sa.func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.revoked_at.is_(None))
    )
    assert live == 0
    assert await db.scalar(
        sa.select(AuditEvent.id).where(AuditEvent.event_type == "auth.refresh_token_reuse_detected")
    )


async def test_unknown_refresh_token_rejected(client: AsyncClient) -> None:
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": "x" * 43})
    assert r.status_code == 401


async def test_logout_revokes_session_and_is_idempotent(api: Api, client: AsyncClient) -> None:
    s = await api.user()
    assert (
        await client.post("/api/v1/auth/logout", json={"refresh_token": s.refresh_token})
    ).status_code == 204
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": s.refresh_token})
    assert r.status_code == 401
    # Second logout and unknown tokens are silent no-ops (no validity oracle).
    assert (
        await client.post("/api/v1/auth/logout", json={"refresh_token": s.refresh_token})
    ).status_code == 204
    assert (
        await client.post("/api/v1/auth/logout", json={"refresh_token": "y" * 43})
    ).status_code == 204


async def test_logout_only_kills_its_own_session(api: Api, client: AsyncClient) -> None:
    s = await api.user()
    second = (await api.login(s.email, DEFAULT_PASSWORD)).json()  # e.g. another device
    await client.post("/api/v1/auth/logout", json={"refresh_token": s.refresh_token})
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 200


async def test_extra_fields_rejected(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "x@example.com",
            "password": DEFAULT_PASSWORD,
            "display_name": "x",
            "is_active": False,
        },
    )
    assert r.status_code == 422
