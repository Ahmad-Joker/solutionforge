"""Entry point: ``python -m solutionforge.worker``."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.config import get_settings
from solutionforge.core.logging import configure_logging, get_logger
from solutionforge.db.session import build_engine, build_sessionmaker
from solutionforge.llm.factory import build_llm_service
from solutionforge.retrieval.factory import build_retriever
from solutionforge.retrieval.ingest import IngestionWorker
from solutionforge.services.approval_service import expire_due
from solutionforge.tools.factory import build_tool_executor
from solutionforge.workflows.engine import Engine
from solutionforge.workflows.registry import StepRegistry
from solutionforge.workflows.steps import default_registry
from solutionforge.workflows.worker import Worker

log = get_logger("solutionforge.worker")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    db = build_engine(settings.database_url)
    sessionmaker = build_sessionmaker(db)
    retriever = build_retriever(sessionmaker)
    registry = default_registry(
        build_llm_service(settings, sessionmaker),
        build_tool_executor(settings, sessionmaker),
        retriever,
    )
    engine = Engine(sessionmaker, registry)
    ingestion = IngestionWorker(sessionmaker, retriever.embedder, worker_id=engine.worker_id)
    worker = Worker(
        engine,
        concurrency=settings.worker_concurrency,
        poll_interval=settings.worker_poll_interval_seconds,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no add_signal_handler; Ctrl+C raises KeyboardInterrupt instead.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        # One process, two durable job loops: workflow executions and document ingestion.
        await asyncio.gather(
            worker.run(stop),
            ingestion.run(stop, poll_interval=settings.worker_poll_interval_seconds),
            _expire_approvals(stop, sessionmaker, registry),
        )
    finally:
        await db.dispose()


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


if __name__ == "__main__":
    asyncio.run(main())
