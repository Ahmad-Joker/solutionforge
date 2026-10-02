"""Connection-pool safety: no request may hold one pool connection while waiting for another.

Load testing found knowledge-base search doing exactly that (request session open, then the
retriever's own session): under concurrency every connection ended "idle in transaction"
and the whole API stalled. With a one-connection pool, any such nesting deadlocks at once,
so these tests catch the bug class, not just the one instance.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from solutionforge.core.config import Settings
from solutionforge.main import create_app
from solutionforge.retrieval.ingest import IngestionWorker
from tests.helpers import Api
from tests.integration.test_knowledge import kb_with_corpus


@pytest.fixture
async def tiny_pool_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(
        settings.model_copy(
            update={
                "database_pool_size": 1,
                "database_max_overflow": 0,
                "database_pool_timeout_seconds": 3,
            }
        )
    )
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await app.state.engine.dispose()


async def test_concurrent_searches_never_need_two_connections(
    api: Api, client: AsyncClient, ingestor: IngestionWorker, tiny_pool_client: AsyncClient
) -> None:
    owner, org, kb, _ = await kb_with_corpus(api, client, ingestor)
    url = f"/api/v1/orgs/{org}/knowledge-bases/{kb}/search"

    async def one(strategy: str) -> int:
        r = await tiny_pool_client.post(
            url, json={"query": "refund delay", "strategy": strategy}, headers=owner.headers
        )
        return r.status_code

    statuses = await asyncio.gather(*(one(s) for s in ("hybrid", "dense", "keyword") * 3))
    assert statuses == [200] * 9


async def test_pool_exhaustion_is_a_503_with_retry_after(
    api: Api, client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    owner = await api.user()
    org = await api.org(owner)
    busy = create_app(
        settings.model_copy(
            update={
                "database_pool_size": 1,
                "database_max_overflow": 0,
                "database_pool_timeout_seconds": 0.5,
            }
        )
    )
    async with busy.state.engine.connect():  # hold the only connection
        transport = ASGITransport(app=busy, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(f"/api/v1/orgs/{org}/workflows", headers=owner.headers)
    await busy.state.engine.dispose()
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "server_busy" and r.headers["retry-after"] == "1"
