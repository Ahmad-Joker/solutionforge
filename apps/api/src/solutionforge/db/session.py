"""Engine and session factory construction."""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from solutionforge.core.text import scrub_nul


def json_serializer(value: Any) -> str:
    """JSON columns: user input with NUL is rejected at the API, but model and connector
    output is not ours to reject — scrub it so PostgreSQL can always store a checkpoint."""
    return json.dumps(scrub_nul(value))


def build_engine(
    url: str, *, echo: bool = False, pool_size: int = 10, max_overflow: int = 20
) -> AsyncEngine:
    kwargs: dict[str, object] = {"echo": echo, "json_serializer": json_serializer}
    if url.startswith("sqlite"):
        # SQLite is only used for local tests; enforce FKs so cascade/orphan bugs surface.
        engine = create_async_engine(url, **kwargs)

        @sa.event.listens_for(engine.sync_engine, "connect")
        def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    # pre_ping: serverless Postgres (e.g. Neon) suspends when idle and drops connections.
    kwargs.update(pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow)
    return create_async_engine(url, **kwargs)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
