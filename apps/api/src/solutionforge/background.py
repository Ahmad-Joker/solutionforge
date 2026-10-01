"""The background job loops, shared by the worker process and the API's embedded mode.

Three durable loops, all lease-based and safe to run in many processes at once:
workflow executions, document ingestion, and approval expiry.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.config import Settings
from solutionforge.core.logging import get_logger
from solutionforge.retrieval.ingest import IngestionWorker
from solutionforge.retrieval.search import Retriever
from solutionforge.services.approval_service import expire_due
from solutionforge.workflows.engine import Engine
from solutionforge.workflows.registry import StepRegistry
from solutionforge.workflows.worker import Worker

log = get_logger(__name__)


async def run_background(
    stop: asyncio.Event,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: StepRegistry,
    retriever: Retriever,
) -> None:
    engine = Engine(sessionmaker, registry)
    worker = Worker(
        engine,
        concurrency=settings.worker_concurrency,
        poll_interval=settings.worker_poll_interval_seconds,
    )
    ingestion = IngestionWorker(sessionmaker, retriever.embedder, worker_id=engine.worker_id)
    await asyncio.gather(
        worker.run(stop),
        ingestion.run(stop, poll_interval=settings.worker_poll_interval_seconds),
        _expire_approvals(stop, sessionmaker, registry),
    )


async def _expire_approvals(
    stop: asyncio.Event, sessionmaker: async_sessionmaker[AsyncSession], registry: StepRegistry
) -> None:
    """Every 30 s: expire overdue approvals and resume their executions."""
    while not stop.is_set():
        try:
            await expire_due(sessionmaker, registry)
        except Exception:
            log.exception("approval_expiry_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=30)
