"""Entry point: ``python -m solutionforge.worker``."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from solutionforge.core.config import get_settings
from solutionforge.core.logging import configure_logging
from solutionforge.db.session import build_engine, build_sessionmaker
from solutionforge.llm.factory import build_llm_service
from solutionforge.tools.factory import build_tool_executor
from solutionforge.workflows.engine import Engine
from solutionforge.workflows.steps import default_registry
from solutionforge.workflows.worker import Worker


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    db = build_engine(settings.database_url)
    sessionmaker = build_sessionmaker(db)
    registry = default_registry(
        build_llm_service(settings, sessionmaker), build_tool_executor(settings, sessionmaker)
    )
    engine = Engine(sessionmaker, registry)
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
        await worker.run(stop)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
