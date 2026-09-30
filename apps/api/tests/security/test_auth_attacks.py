from __future__ import annotations

import uuid

import jwt
import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.config import Settings
from solutionforge.security.tokens import issue_access_token
from tests.helpers import Api

pytestmark = pytest.mark.security


@pytest.mark.parametrize(
    "header",
    [None, "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "Bearer not-a-jwt", "Token abc.def.ghi"],
)
async def test_protected_routes_require_valid_bearer(
    client: AsyncClient, header: str | None
) -> None:
    headers = {"Authorization": header} if header is not None else {}
    r = await client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "authentication_failed"


async def test_refresh_token_cannot_be_used_as_access_token(api: Api, client: AsyncClient) -> None:
    s = await api.user()
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {s.refresh_token}"})
    assert r.status_code == 401


async def test_valid_signature_for_deleted_user_rejected(
    client: AsyncClient, settings: Settings
) -> None:
    token = issue_access_token(uuid.uuid4(), settings).token
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_token_signed_with_foreign_secret_rejected(api: Api, client: AsyncClient) -> None:
    s = await api.user()
    claims = jwt.decode(s.access_token, options={"verify_signature": False})
    forged = jwt.encode(claims, "attacker-controlled-secret-with-32-chars!", algorithm="HS256")
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


async def test_sql_metacharacters_in_email_are_inert(api: Api) -> None:
    r = await api.login("' OR 1=1 --@example.com", "x")
    assert r.status_code in (401, 422)


async def test_oversized_password_rejected_before_hashing(client: AsyncClient) -> None:
    """Argon2 on a multi-megabyte input is a cheap DoS; the schema caps it first."""
    r = await client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "x" * 100_000}
    )
    assert r.status_code == 422


async def test_audit_log_is_append_only_in_postgres(api: Api, db: AsyncSession) -> None:
    if db.bind.dialect.name != "postgresql":
        pytest.skip("append-only trigger is PostgreSQL-specific; enforced in CI")
    await api.user()
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(sa.text("UPDATE audit_events SET event_type = 'tampered'"))
    await db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(sa.text("DELETE FROM audit_events"))
    await db.rollback()
