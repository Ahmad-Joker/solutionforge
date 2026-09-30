"""Durable workflow engine.

Execution model (see ARCHITECTURE.md §6 and ADR-0003):

1. **Claim.** A worker leases a runnable execution (``queued`` and due, or ``running`` with
   an expired lease, i.e. its worker died) using ``FOR UPDATE SKIP LOCKED`` + compare-and-set.
2. **Prepare** (short transaction). Check cancellation and budgets, bump the attempt counter
   and extend the lease to cover the step's timeout. The attempt is counted *before* running,
   so a step that repeatedly crashes its worker still exhausts its attempts (poison pill guard).
3. **Run** the step handler with *no* database connection held, under ``asyncio.timeout``.
4. **Checkpoint** (one transaction): a fenced ``UPDATE … WHERE lease_owner = me`` plus the
   ``execution_steps`` row. If the lease was lost to another worker the update matches no
   row and the whole checkpoint is discarded, so a zombie worker can never overwrite progress.

A crash between 2 and 4 loses only the in-flight attempt; the next worker re-runs that step
with the same idempotency key.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.clock import utcnow
from solutionforge.core.logging import get_logger
from solutionforge.domain.workflow import (
    Execution,
    ExecutionStatus,
    ExecutionStep,
    WorkflowVersion,
)
from solutionforge.workflows import transitions
from solutionforge.workflows.definition import (
    CompiledStep,
    CompiledWorkflow,
    DefinitionInvalid,
    compile_definition,
)
from solutionforge.workflows.registry import StepContext, StepError, StepRegistry, StepResult

log = get_logger(__name__)


class LeaseLost(Exception):
    """Another worker owns this execution now; stop without writing anything."""


class _Again:
    """Sentinel: state changed without running a step (e.g. routed to on_error); loop again."""


AGAIN = _Again()


@dataclass(frozen=True, slots=True)
class EngineConfig:
    claim_lease_seconds: float = 60.0
    lease_margin_seconds: float = 30.0
    compile_cache_size: int = 512


def default_worker_id() -> str:
    return f"{socket.gethostname()[:32]}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Engine:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        registry: StepRegistry,
        *,
        worker_id: str | None = None,
        config: EngineConfig | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.registry = registry
        self.worker_id = worker_id or default_worker_id()
        self.config = config or EngineConfig()
        self._compiled: dict[uuid.UUID, CompiledWorkflow] = {}

    # ------------------------------------------------------------------ public API

    async def claim(self) -> uuid.UUID | None:
        """Lease one runnable execution, or return None if nothing is due."""
        now = utcnow()
        runnable = sa.or_(
            sa.and_(Execution.status == ExecutionStatus.QUEUED, Execution.run_after <= now),
            sa.and_(Execution.status == ExecutionStatus.RUNNING, Execution.lease_expires_at < now),
        )
        async with self.sessionmaker() as s, s.begin():
            candidate = await s.scalar(
                sa.select(Execution.id)
                .where(runnable)
                .order_by(Execution.run_after)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if candidate is None:
                return None
            result = await s.execute(
                sa.update(Execution)
                .where(Execution.id == candidate, runnable)
                .values(
                    status=ExecutionStatus.RUNNING,
                    lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.config.claim_lease_seconds),
                    started_at=sa.func.coalesce(Execution.started_at, now),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                return None  # lost a race on SQLite (no SKIP LOCKED); try again later
        log.info("execution_claimed", execution_id=str(candidate), worker=self.worker_id)
        return candidate

    async def run(self, execution_id: uuid.UUID) -> None:
        """Drive a claimed execution until it finishes, suspends, schedules a retry, or is
        taken over. Bounded by the definition's max_steps (checked every iteration)."""
        try:
            while await self._advance(execution_id):
                pass
        except LeaseLost:
            log.warning("lease_lost", execution_id=str(execution_id), worker=self.worker_id)

    async def run_until_idle(self, max_executions: int = 1000) -> int:
        """Process everything currently runnable (tests, CLI). Returns executions processed."""
        processed = 0
        while processed < max_executions:
            execution_id = await self.claim()
            if execution_id is None:
                return processed
            await self.run(execution_id)
            processed += 1
        return processed

    async def compiled_for(self, session: AsyncSession, version_id: uuid.UUID) -> CompiledWorkflow:
        cached = self._compiled.get(version_id)
        if cached is not None:
            return cached
        version = await session.get(WorkflowVersion, version_id)
        if version is None:
            raise DefinitionInvalid([{"loc": [], "msg": "workflow version not found"}])
        compiled = compile_definition(version.definition, self.registry)
        if len(self._compiled) >= self.config.compile_cache_size:
            self._compiled.pop(next(iter(self._compiled)))
        self._compiled[version_id] = compiled  # versions are immutable: safe to cache forever
        return compiled

    # ------------------------------------------------------------------ one step

    async def _advance(self, execution_id: uuid.UUID) -> bool:
        prepared = await self._prepare(execution_id)
        if prepared is None:
            return False
        if isinstance(prepared, _Again):
            return True
        compiled, step, ctx, snap = prepared

        started = utcnow()
        t0 = time.monotonic()
        result: StepResult | None = None
        failure: StepError | None = None
        try:
            async with asyncio.timeout(step.spec.timeout_seconds):
                result = await step.handler.run(step.config, ctx)
        except TimeoutError:
            failure = StepError(
                f"step timed out after {step.spec.timeout_seconds}s", retryable=True, code="timeout"
            )
        except StepError as exc:
            failure = exc
        except Exception as exc:  # handler bug or unexpected dependency failure
            log.exception("step_unhandled_exception", step_id=step.spec.id)
            failure = StepError(
                f"unhandled {type(exc).__name__} in step",
                retryable=True,
                code="unhandled_error",
                details={"exception_type": type(exc).__name__},
            )
        duration_ms = int((time.monotonic() - t0) * 1000)

        now = utcnow()
        if failure is not None:
            tr = transitions.on_failure(
                snap,
                step,
                failure,
                attempt=ctx.attempt,
                now=now,
                continue_status=ExecutionStatus.RUNNING,
            )
        else:
            assert result is not None
            if result.suspend is not None:
                tr = transitions.on_suspend(result, now=now)
            else:
                tr = transitions.on_success(
                    snap,
                    compiled,
                    step,
                    result,
                    now=now,
                    attempt=ctx.attempt,
                    continue_status=ExecutionStatus.RUNNING,
                )
        await self._checkpoint(execution_id, step, ctx.attempt, tr, started, duration_ms)
        log.info(
            "step_finished",
            execution_id=str(execution_id),
            step_id=step.spec.id,
            attempt=ctx.attempt,
            status=tr.step_status.value,
            duration_ms=duration_ms,
            next_status=str(tr.values.get("status", ExecutionStatus.RUNNING)),
            **tr.extra,
        )
        return bool(tr.values.get("status", ExecutionStatus.RUNNING) == ExecutionStatus.RUNNING)

    async def _prepare(
        self, execution_id: uuid.UUID
    ) -> tuple[CompiledWorkflow, CompiledStep, StepContext, transitions.Snapshot] | _Again | None:
        async with self.sessionmaker() as s, s.begin():
            ex = await s.get(Execution, execution_id, populate_existing=True)
            if (
                ex is None
                or ex.status != ExecutionStatus.RUNNING
                or ex.lease_owner != self.worker_id
            ):
                return None
            now = utcnow()

            try:
                compiled = await self.compiled_for(s, ex.workflow_version_id)
            except DefinitionInvalid as exc:
                await self._finish(
                    s,
                    ex,
                    ExecutionStatus.FAILED,
                    now,
                    {
                        "code": "definition_invalid",
                        "message": exc.message,
                        "details": exc.details,
                    },
                )
                return None

            if ex.cancel_requested:
                await self._finish(s, ex, ExecutionStatus.CANCELLED, now, {"code": "cancelled"})
                return None

            limits = compiled.definition.limits
            if ex.steps_used >= limits.max_steps:
                await self._finish(
                    s,
                    ex,
                    ExecutionStatus.BUDGET_EXCEEDED,
                    now,
                    {
                        "code": "max_steps_exceeded",
                        "message": f"step budget of {limits.max_steps} exhausted",
                    },
                )
                return None
            if ex.active_ms >= limits.max_active_seconds * 1000:
                await self._finish(
                    s,
                    ex,
                    ExecutionStatus.BUDGET_EXCEEDED,
                    now,
                    {
                        "code": "max_active_time_exceeded",
                        "message": f"active time budget of {limits.max_active_seconds}s exhausted",
                    },
                )
                return None

            assert ex.current_step is not None
            step = compiled.steps[ex.current_step]
            snap = transitions.Snapshot.of(ex.input, ex.state, ex.step_outputs)
            attempt = ex.current_attempt + 1

            if attempt > step.spec.retry.max_attempts:
                # The previous attempt never checkpointed (worker died mid-step).
                err = StepError(
                    "step did not complete within its attempts (worker lost mid-step)",
                    code="attempts_exhausted",
                )
                tr = transitions.on_failure(
                    snap,
                    step,
                    err,
                    attempt=ex.current_attempt,
                    now=now,
                    continue_status=ExecutionStatus.RUNNING,
                )
                await self._write(
                    s, ex.id, ex.organization_id, step, ex.current_attempt, tr, now, 0
                )
                return AGAIN if tr.values.get("status") == ExecutionStatus.RUNNING else None

            lease_until = now + timedelta(
                seconds=step.spec.timeout_seconds + self.config.lease_margin_seconds
            )
            await self._fenced_update(
                s,
                ex.id,
                {
                    "current_attempt": attempt,
                    "steps_used": Execution.steps_used + 1,
                    "lease_expires_at": lease_until,
                    "updated_at": now,
                },
            )
            ctx = StepContext(
                organization_id=ex.organization_id,
                execution_id=ex.id,
                workflow_id=ex.workflow_id,
                step_id=step.spec.id,
                attempt=attempt,
                scope=snap.handler_scope(),
                deadline=time.monotonic() + step.spec.timeout_seconds,
                cost_limit_micro_usd=limits.max_cost_micro_usd,
                llm_token_limit=limits.max_llm_tokens,
            )
            return compiled, step, ctx, snap

    # ------------------------------------------------------------------ persistence

    async def _checkpoint(
        self,
        execution_id: uuid.UUID,
        step: CompiledStep,
        attempt: int,
        tr: transitions.Transition,
        started: Any,
        duration_ms: int,
    ) -> None:
        async with self.sessionmaker() as s, s.begin():
            org_id = await s.scalar(
                sa.select(Execution.organization_id).where(Execution.id == execution_id)
            )
            if org_id is None:
                raise LeaseLost
            await self._write(s, execution_id, org_id, step, attempt, tr, started, duration_ms)

    async def _write(
        self,
        s: AsyncSession,
        execution_id: uuid.UUID,
        organization_id: uuid.UUID,
        step: CompiledStep,
        attempt: int,
        tr: transitions.Transition,
        started: Any,
        duration_ms: int,
    ) -> None:
        values = dict(tr.values)
        values["active_ms"] = Execution.active_ms + duration_ms
        values["event_seq"] = Execution.event_seq + 1
        seq = await self._fenced_update(s, execution_id, values, returning_seq=True)
        s.add(
            ExecutionStep(
                organization_id=organization_id,
                execution_id=execution_id,
                seq=seq,
                step_id=step.spec.id,
                step_type=step.spec.type,
                attempt=attempt,
                status=tr.step_status,
                worker_id=self.worker_id,
                output=tr.step_output,
                error=tr.step_error,
                started_at=started,
                finished_at=utcnow(),
                duration_ms=duration_ms,
            )
        )

    async def _fenced_update(
        self,
        s: AsyncSession,
        execution_id: uuid.UUID,
        values: dict[str, Any],
        *,
        returning_seq: bool = False,
    ) -> int:
        stmt = (
            sa.update(Execution)
            .where(
                Execution.id == execution_id,
                Execution.lease_owner == self.worker_id,
                Execution.status == ExecutionStatus.RUNNING,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if returning_seq:
            seq = await s.scalar(stmt.returning(Execution.event_seq))
            if seq is None:
                raise LeaseLost
            return int(seq)
        result = await s.execute(stmt)
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise LeaseLost
        return 0

    async def _finish(
        self,
        s: AsyncSession,
        ex: Execution,
        status: ExecutionStatus,
        now: Any,
        error: dict[str, Any],
    ) -> None:
        await self._fenced_update(s, ex.id, transitions.terminal_values(status, now, error=error))
        log.info("execution_finished", execution_id=str(ex.id), status=status.value, **error)
