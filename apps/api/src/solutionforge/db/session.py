"""Engine and session factory construction."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    kwargs: dict[str, object] = {"echo": echo}
    if url.startswith("sqlite"):
        # SQLite is only used for local tests; enforce FKs so cascade/orphan bugs surface.
        engine = create_async_engine(url, **kwargs)

        @sa.event.listens_for(engine.sync_engine, "connect")
        def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)
    return create_async_engine(url, **kwargs)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
