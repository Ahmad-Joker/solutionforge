"""Pure state-transition rules: (execution snapshot, step outcome) → new column values.

Shared by the engine (worker path) and the resume service (human-signal path), so both
apply identical semantics, and unit-testable without a database.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from solutionforge.domain.workflow import ExecutionStatus, StepStatus
from solutionforge.workflows import expressions
from solutionforge.workflows.definition import CompiledStep, CompiledWorkflow
from solutionforge.workflows.registry import USE_DEFAULT, StepError, StepResult

MAX_STEP_OUTPUT_BYTES = 256 * 1024
MAX_STATE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Snapshot:
    input: dict[str, Any]
    state: dict[str, Any]
    step_outputs: dict[str, Any]

    @classmethod
    def of(cls, input_: dict[str, Any], state: dict[str, Any], outputs: dict[str, Any]) -> Snapshot:
        """Deep-copied from the ORM row so later mutations of either side don't alias."""
        return cls(copy.deepcopy(input_), copy.deepcopy(state), copy.deepcopy(outputs))

    @property
    def scope(self) -> expressions.Scope:
        return {"input": self.input, "state": self.state, "steps": self.step_outputs}

    def handler_scope(self) -> expressions.Scope:
        """A private copy for step handlers: whatever a handler does to it cannot reach the
        values the engine merges and persists."""
        return copy.deepcopy(self.scope)


@dataclass(slots=True)
class Transition:
    values: dict[str, Any]  # columns to write on the execution
    step_status: StepStatus
    step_output: dict[str, Any] | None = None
    step_error: dict[str, Any] | None = None
    terminal: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def error_dict(err: StepError, *, step_id: str, attempt: int) -> dict[str, Any]:
    return {
        "code": err.code,
        "message": err.message,
        "retryable": err.retryable,
        "step_id": step_id,
        "attempt": attempt,
        "details": err.details,
    }


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str).encode())


def terminal_values(
    status: ExecutionStatus,
    now: datetime,
    *,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "output": output,
        "error": error,
        "finished_at": now,
        "waiting_on": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "updated_at": now,
    }


def on_success(
    snap: Snapshot,
    compiled: CompiledWorkflow,
    step: CompiledStep,
    result: StepResult,
    *,
    now: datetime,
    continue_status: ExecutionStatus,
    attempt: int = 0,
    step_status: StepStatus = StepStatus.SUCCEEDED,
) -> Transition:
    """Apply a successful step: merge state, record output, move to the next step or finish."""
    step_id = step.spec.id
    if _size(result.output) > MAX_STEP_OUTPUT_BYTES:
        return on_failure(
            snap,
            step,
            StepError("step output exceeds size limit", code="output_too_large"),
            attempt=attempt,
            now=now,
            continue_status=continue_status,
            final=True,
        )
    state = {**snap.state, **result.set_state}
    if _size(state) > MAX_STATE_BYTES:
        return on_failure(
            snap,
            step,
            StepError("workflow state exceeds size limit", code="state_too_large"),
            attempt=attempt,
            now=now,
            continue_status=continue_status,
            final=True,
        )
    outputs = {**snap.step_outputs, step_id: result.output}
    nxt = step.spec.next if result.goto is USE_DEFAULT else result.goto
    base = {"state": state, "step_outputs": outputs, "waiting_on": None, "updated_at": now}

    if nxt is None:
        final = Snapshot(snap.input, state, outputs)
        try:
            output = expressions.resolve(compiled.definition.output, final.scope)
        except expressions.ExpressionError as exc:
            err = {"code": "output_resolution_error", "message": str(exc), "step_id": step_id}
            return Transition(
                values=base | terminal_values(ExecutionStatus.FAILED, now, error=err),
                step_status=step_status,
                step_output=result.output,
                terminal=True,
            )
        return Transition(
            values=base | terminal_values(ExecutionStatus.SUCCEEDED, now, output=output),
            step_status=step_status,
            step_output=result.output,
            terminal=True,
        )
    return Transition(
        values=base
        | {
            "current_step": nxt,
            "current_attempt": 0,
            "status": continue_status,
            "run_after": now,
        },
        step_status=step_status,
        step_output=result.output,
    )


def on_suspend(result: StepResult, *, now: datetime) -> Transition:
    assert result.suspend is not None
    waiting = {"reason": result.suspend.reason, **result.suspend.details}
    return Transition(
        values={
            "status": ExecutionStatus.WAITING,
            "waiting_on": waiting,
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": now,
        },
        step_status=StepStatus.WAITING,
        step_output=waiting,
    )


def on_failure(
    snap: Snapshot,
    step: CompiledStep,
    err: StepError,
    *,
    attempt: int,
    now: datetime,
    continue_status: ExecutionStatus,
    step_status: StepStatus = StepStatus.FAILED,
    final: bool = False,
) -> Transition:
    """Retry with backoff, route to ``on_error``, or fail the execution.

    ``final=True`` skips retries (the failure is deterministic, e.g. oversized output).
    """
    spec = step.spec
    error = error_dict(err, step_id=spec.id, attempt=attempt)

    if err.exhausts_budget:
        return Transition(
            values=terminal_values(ExecutionStatus.BUDGET_EXCEEDED, now, error=error),
            step_status=step_status,
            step_error=error,
            terminal=True,
        )

    if err.retryable and not final and 0 < attempt < spec.retry.max_attempts:
        delay = spec.retry.backoff_seconds(attempt)
        return Transition(
            values={
                "status": ExecutionStatus.QUEUED,
                "run_after": now + timedelta(seconds=delay),
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            },
            step_status=step_status,
            step_error=error,
            extra={"retry_in_seconds": delay},
        )

    if spec.on_error is not None:
        # Expose the failure to the fallback step as $.steps.<id>.error
        outputs = {**snap.step_outputs, spec.id: {"error": error}}
        return Transition(
            values={
                "step_outputs": outputs,
                "current_step": spec.on_error,
                "current_attempt": 0,
                "status": continue_status,
                "run_after": now,
                "waiting_on": None,
                "updated_at": now,
            },
            step_status=step_status,
            step_error=error,
            extra={"routed_to": spec.on_error},
        )

    return Transition(
        values=terminal_values(ExecutionStatus.FAILED, now, error=error),
        step_status=step_status,
        step_error=error,
        terminal=True,
    )
