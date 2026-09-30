from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from solutionforge.core.errors import ValidationFailed
from solutionforge.domain.workflow import ExecutionStatus, StepStatus
from solutionforge.workflows import transitions
from solutionforge.workflows.definition import (
    DefinitionInvalid,
    RetryPolicy,
    compile_definition,
    validate_input,
)
from solutionforge.workflows.registry import StepError, StepResult
from solutionforge.workflows.steps import default_registry

REG = default_registry()
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def simple(**overrides: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "start": "a",
        "inputs": {"name": {"type": "string"}},
        "steps": [
            {
                "id": "a",
                "type": "transform",
                "config": {"set": {"who": "$.input.name"}},
                "next": "b",
            },
            {
                "id": "b",
                "type": "condition",
                "config": {
                    "branches": [
                        {"when": {"path": "$.state.who", "op": "eq", "value": "x"}, "goto": "c"}
                    ],
                    "default": None,
                },
            },
            {"id": "c", "type": "fail", "config": {"message": "no x allowed"}},
        ],
        "output": {"greeting": "hello {{ $.state.who }}"},
    }
    d.update(overrides)
    return d


def errors_of(defn: dict[str, Any]) -> list[str]:
    with pytest.raises(DefinitionInvalid) as ei:
        compile_definition(defn, REG)
    return [e["msg"] for e in ei.value.errors]


def test_valid_definition_compiles_and_hash_is_stable() -> None:
    a = compile_definition(simple(), REG)
    b = compile_definition(simple(), REG)
    assert set(a.steps) == {"a", "b", "c"}
    assert a.hash() == b.hash()
    # Canonical form fills defaults, so semantically-equal definitions hash equally.
    assert a.canonical_json()["steps"][0]["retry"]["max_attempts"] == 1


def test_dangling_targets_rejected() -> None:
    d = simple()
    d["steps"][0]["next"] = "nowhere"
    assert any("unknown step 'nowhere'" in m for m in errors_of(d))


def test_missing_start_rejected() -> None:
    assert any("start step" in m for m in errors_of(simple(start="zzz")))


def test_unreachable_steps_rejected() -> None:
    d = simple()
    d["steps"].append({"id": "orphan", "type": "transform"})
    assert any("unreachable" in m for m in errors_of(d))


def test_duplicate_ids_rejected() -> None:
    d = simple()
    d["steps"].append({"id": "a", "type": "transform"})
    assert any("duplicate" in m for m in errors_of(d))


def test_unknown_step_type_and_bad_config() -> None:
    d = simple()
    d["steps"][0]["type"] = "shell_exec"
    d["steps"][2]["config"] = {"message": "", "code": "Bad Code"}
    msgs = errors_of(d)
    assert any("unknown step type 'shell_exec'" in m for m in msgs)
    assert len(msgs) >= 2  # all problems reported at once, not just the first


def test_reference_validation() -> None:
    d = simple(output={"x": "$.steps.ghost.value", "y": "$.input.undeclared", "z": "$.secret"})
    msgs = errors_of(d)
    assert any("unknown step" in m for m in msgs)
    assert any("undeclared input" in m for m in msgs)
    assert any("root must be" in m for m in msgs)


def test_invalid_state_variable_name() -> None:
    d = simple()
    d["steps"][0]["config"]["set"] = {"not valid!": 1}
    assert any("invalid state variable" in m for m in errors_of(d))


def test_limits_are_capped() -> None:
    assert errors_of(simple(limits={"max_steps": 100_000}))


def test_extra_fields_rejected() -> None:
    assert errors_of(simple(eval="__import__('os')"))


def test_self_error_handler_rejected() -> None:
    d = simple()
    d["steps"][0]["on_error"] = "a"
    assert any("own error handler" in m for m in errors_of(d))


# ----------------------------------------------------------------- input validation


@pytest.mark.parametrize(
    ("data", "ok"),
    [
        ({"name": "x"}, True),
        ({}, False),
        ({"name": 5}, False),
        ({"name": "x", "extra": 1}, False),
    ],
)
def test_validate_input(data: dict[str, Any], ok: bool) -> None:
    defn = compile_definition(simple(), REG).definition
    if ok:
        validate_input(defn, data)
    else:
        with pytest.raises(ValidationFailed):
            validate_input(defn, data)


def test_boolean_is_not_a_number() -> None:
    defn = compile_definition(
        simple(inputs={"name": {"type": "string"}, "n": {"type": "number"}}, output={}), REG
    ).definition
    with pytest.raises(ValidationFailed):
        validate_input(defn, {"name": "x", "n": True})


# ----------------------------------------------------------------- retry + transitions


def test_backoff_is_exponential_and_capped() -> None:
    p = RetryPolicy(max_attempts=6, initial_backoff_seconds=1, multiplier=3, max_backoff_seconds=20)
    assert [p.backoff_seconds(a) for a in range(1, 6)] == [1, 3, 9, 20, 20]


def _step(**spec: Any) -> Any:
    base = {
        "start": "a",
        "steps": [{"id": "a", "type": "transform", **spec}, {"id": "fb", "type": "transform"}],
    }
    base["steps"][0].setdefault("next", "fb")
    return compile_definition(base, REG)


SNAP = transitions.Snapshot({}, {"k": 1}, {})


def test_retryable_failure_schedules_retry() -> None:
    wf = _step(retry={"max_attempts": 3, "initial_backoff_seconds": 5})
    tr = transitions.on_failure(
        SNAP,
        wf.steps["a"],
        StepError("x", retryable=True),
        attempt=1,
        now=NOW,
        continue_status=ExecutionStatus.RUNNING,
    )
    assert tr.values["status"] == ExecutionStatus.QUEUED
    assert (tr.values["run_after"] - NOW).total_seconds() == 5
    assert tr.step_status == StepStatus.FAILED and not tr.terminal


def test_exhausted_retries_route_to_on_error_with_error_in_scope() -> None:
    wf = _step(retry={"max_attempts": 2}, on_error="fb")
    tr = transitions.on_failure(
        SNAP,
        wf.steps["a"],
        StepError("down", retryable=True, code="timeout"),
        attempt=2,
        now=NOW,
        continue_status=ExecutionStatus.RUNNING,
    )
    assert tr.values["current_step"] == "fb"
    assert tr.values["step_outputs"]["a"]["error"]["code"] == "timeout"


def test_non_retryable_without_handler_fails_execution() -> None:
    wf = _step(retry={"max_attempts": 5})
    tr = transitions.on_failure(
        SNAP,
        wf.steps["a"],
        StepError("bad input"),
        attempt=1,
        now=NOW,
        continue_status=ExecutionStatus.RUNNING,
    )
    assert tr.terminal and tr.values["status"] == ExecutionStatus.FAILED
    assert tr.values["lease_owner"] is None


def test_success_merges_state_and_advances() -> None:
    wf = _step()
    tr = transitions.on_success(
        SNAP,
        wf,
        wf.steps["a"],
        StepResult(output={"o": 1}, set_state={"j": 2}),
        now=NOW,
        continue_status=ExecutionStatus.RUNNING,
    )
    assert tr.values["state"] == {"k": 1, "j": 2}
    assert tr.values["current_step"] == "fb" and tr.values["current_attempt"] == 0


def test_oversized_output_fails_without_retry() -> None:
    wf = _step(retry={"max_attempts": 5})
    big = {"blob": "x" * (transitions.MAX_STEP_OUTPUT_BYTES + 1)}
    tr = transitions.on_success(
        SNAP,
        wf,
        wf.steps["a"],
        StepResult(output=big),
        now=NOW,
        attempt=1,
        continue_status=ExecutionStatus.RUNNING,
    )
    assert tr.terminal and tr.values["error"]["code"] == "output_too_large"


def test_snapshot_is_isolated_from_source() -> None:
    state = {"nested": {"v": 1}}
    snap = transitions.Snapshot.of({}, state, {})
    snap.scope["state"]["nested"]["v"] = 999
    assert state["nested"]["v"] == 1
