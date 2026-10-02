"""Close hosted Data APIs (Supabase/PostgREST) over our tables.

Supabase exposes the ``public`` schema via PostgREST and by default grants its ``anon`` and
``authenticated`` roles access to every new table. SolutionForge never uses that API (tenancy
and RBAC are enforced in its own service layer), so those grants would let anyone holding the
public-by-design anon key bypass all of it. Verified: without this, all tables — including
``users`` — were readable and writable by ``anon``.

This revokes those grants and the default privileges for future tables created by the
migrating role. It is a no-op where the roles don't exist (plain PostgreSQL, SQLite). Later
migrations stay covered by the default-privilege change; ``infra/supabase/lockdown.sql`` is
the same lockdown as a manual, re-runnable script.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCKDOWN = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
      EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', r);
      EXECUTE format('REVOKE ALL ON ALL ROUTINES IN SCHEMA public FROM %I', r);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', r);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I', r);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON ROUTINES FROM %I', r);
    END IF;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_LOCKDOWN)


def downgrade() -> None:
    # Deliberately not re-granting: reopening a public API over every table is never a
    # rollback anyone should get by accident.
    pass
