"""Worker process: claims runnable executions and drives them through the engine.

Run with ``python -m solutionforge.worker``. Many workers can run concurrently; leases make
that safe. On shutdown, in-flight steps are given ``drain_seconds`` to finish; anything
still running is recovered by another worker once its lease expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import uuid

from solutionforge.core.logging import get_logger
from solutionforge.workflows.engine import Engine

log = get_logger(__name__)


class Worker:
    def __init__(
        self,
        engine: Engine,
        *,
        concurrency: int = 4,
        poll_interval: float = 1.0,
        drain_seconds: float = 30.0,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.engine = engine
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.drain_seconds = drain_seconds

    async def run(self, stop: asyncio.Event) -> None:
        slots = asyncio.Semaphore(self.concurrency)
        tasks: set[asyncio.Task[None]] = set()
        log.info("worker_started", worker=self.engine.worker_id, concurrency=self.concurrency)

        while not stop.is_set():
            await slots.acquire()
            try:
                execution_id = await self.engine.claim()
            except Exception:  # DB blip: log, back off, keep the worker alive
                log.exception("claim_failed")
                slots.release()
                await self._sleep(stop, self.poll_interval * 5)
                continue
            if execution_id is None:
                slots.release()
                # Jitter spreads polling from many workers.
                await self._sleep(stop, self.poll_interval * random.uniform(0.5, 1.5))  # noqa: S311
                continue
            task = asyncio.create_task(self._run_one(execution_id, slots))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        if tasks:
            log.info("worker_draining", in_flight=len(tasks))
            _, pending = await asyncio.wait(tasks, timeout=self.drain_seconds)
            for t in pending:
                t.cancel()
        log.info("worker_stopped", worker=self.engine.worker_id)

    async def _run_one(self, execution_id: uuid.UUID, slots: asyncio.Semaphore) -> None:
        try:
            await self.engine.run(execution_id)
        except Exception:  # never let one execution kill the worker
            log.exception("execution_run_failed", execution_id=str(execution_id))
        finally:
            slots.release()

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
