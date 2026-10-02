"""Entry point: ``python -m solutionforge.worker``."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from prometheus_client import start_http_server

from solutionforge.background import run_background
from solutionforge.core.config import get_settings
from solutionforge.core.logging import configure_logging
from solutionforge.db.session import build_engine, build_sessionmaker
from solutionforge.llm.factory import build_llm_service
from solutionforge.observability import metrics
from solutionforge.observability.tracing import configure_tracing
from solutionforge.retrieval.factory import build_retriever
from solutionforge.tools.factory import build_tool_executor
from solutionforge.workflows.steps import default_registry


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    configure_tracing("solutionforge-worker", otlp_endpoint=settings.otel_exporter_otlp_endpoint)
    if settings.worker_metrics_port is not None:
        # Plain scrape port for the worker process; keep it on the private network.
        start_http_server(settings.worker_metrics_port, registry=metrics.REGISTRY)
    db = build_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    sessionmaker = build_sessionmaker(db)
    retriever = build_retriever(sessionmaker)
    registry = default_registry(
        build_llm_service(settings, sessionmaker),
        build_tool_executor(settings, sessionmaker),
        retriever,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no add_signal_handler; Ctrl+C raises KeyboardInterrupt instead.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_background(stop, settings, sessionmaker, registry, retriever)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
