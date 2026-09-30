# Contributing

## Dev loop

```bash
cd apps/api
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest                      # SQLite; set SF_TEST_DATABASE_URL for PostgreSQL
```

## Rules

- **Layering:** `api` (HTTP only) → `services` (use cases, permission checks, commits) →
  `domain` / `db` / `security`. Services must not import FastAPI.
- **Tenancy:** tenant-owned models inherit `TenantScopedMixin`. Query them through
  `scoped_select(Model, ctx, …)`. Never accept `organization_id` from a request body.
  Every new tenant resource gets cases in `tests/security/test_tenant_isolation.py`.
- **Authorization:** call `ensure(ctx, Permission.X)` in the service. New endpoints get a row
  in `tests/security/test_rbac_enforcement.py`.
- **Audit:** security-relevant writes call `audit_service.record(...)` before the commit.
  Never put secrets in metadata (it's redacted anyway, but don't rely on that).
- **Schema changes:** change the model, run
  `alembic revision --autogenerate -m "..."`, review the output, and commit it.
  `test_models_match_migrations` fails if you forget.
- **Time:** use `solutionforge.core.clock.utcnow()`. Naive datetimes are rejected at the DB layer.
- **Errors:** raise `core.errors.*`. Don't return error dicts or raise `HTTPException` from services.
- **No silent failures:** don't use `except Exception: pass`. Don't skip or delete a test to make CI green.

## Commits

Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `ci:`). Each PR updates
docs and ADRs when it changes behavior or architecture.
