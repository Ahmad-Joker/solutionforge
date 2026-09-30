"""Health probes, error envelope, request IDs, and migration/model drift."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi import FastAPI
from httpx import AsyncClient

from solutionforge.db.base import Base


async def test_liveness_and_readiness(client: AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    r = await client.get("/readyz")
    assert r.status_code == 200 and r.json()["checks"]["database"] == "ok"


async def test_readiness_reports_unavailable_database(
    settings, client: AsyncClient, app: FastAPI
) -> None:  # type: ignore[no-untyped-def]
    from solutionforge.db.session import build_engine

    good = app.state.engine
    app.state.engine = build_engine("sqlite+aiosqlite:///Z:/does/not/exist/x.db")
    try:
        r = await client.get("/readyz")
    finally:
        await app.state.engine.dispose()
        app.state.engine = good
    assert r.status_code == 503 and r.json()["checks"]["database"] == "unavailable"


async def test_request_id_propagation(client: AsyncClient) -> None:
    r = await client.get("/healthz", headers={"X-Request-ID": "trace-abc-12345"})
    assert r.headers["x-request-id"] == "trace-abc-12345"
    # Unsafe values (log injection) are replaced, not echoed.
    r = await client.get("/healthz", headers={"X-Request-ID": "bad\nvalue"})
    assert r.headers["x-request-id"] != "bad\nvalue" and len(r.headers["x-request-id"]) == 32


async def test_error_envelope_shape(client: AsyncClient) -> None:
    r = await client.get("/api/v1/does-not-exist")
    err = r.json()["error"]
    assert r.status_code == 404
    assert set(err) == {"code", "message", "details", "request_id"}
    assert err["request_id"] == r.headers["x-request-id"]


async def test_unhandled_exception_returns_opaque_500(app: FastAPI, client: AsyncClient) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    r = await client.get("/boom")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in r.text


async def test_models_match_migrations(app: FastAPI) -> None:
    """Fails if someone changes a model without writing a migration."""

    def diff(sync_conn):  # type: ignore[no-untyped-def]
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    async with app.state.engine.connect() as conn:
        changes = await conn.run_sync(diff)
    changes = [
        c
        for c in changes
        if not (isinstance(c, tuple) and c[0] == "remove_table" and c[1].name == "alembic_version")
    ]
    assert changes == []
