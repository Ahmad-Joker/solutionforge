"""Supabase/PostgREST roles must never hold privileges on our tables (migration 0010)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.security

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0010_close_data_api.py"


def _lockdown_sql() -> str:
    spec = importlib.util.spec_from_file_location("m0010", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._LOCKDOWN)


async def test_supabase_style_grants_are_revoked(db: AsyncSession) -> None:
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL roles and grants")
    await db.execute(
        sa.text(
            "DO $$ BEGIN"
            " IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')"
            " THEN CREATE ROLE anon NOLOGIN; END IF;"
            " IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated')"
            " THEN CREATE ROLE authenticated NOLOGIN; END IF; END $$"
        )
    )
    for role in ("anon", "authenticated"):
        # What Supabase does by default.
        await db.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        await db.execute(sa.text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}"))
        await db.execute(
            sa.text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
        )
    exposed = sa.text("SELECT has_table_privilege('anon', 'public.users', 'SELECT')")
    assert await db.scalar(exposed) is True  # the risk is real

    await db.execute(sa.text(_lockdown_sql()))
    await db.execute(sa.text("CREATE TABLE public.zz_lockdown_probe (id int)"))
    remaining = await db.scalar(
        sa.text(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE table_schema = 'public' AND grantee IN ('anon', 'authenticated')"
        )
    )
    await db.rollback()  # leave the shared test database as it was
    assert remaining == 0
