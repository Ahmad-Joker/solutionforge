"""Engine behaviour against a real (migrated) database: the Phase 2 acceptance criteria."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.domain.workflow import Execution, ExecutionStep
from solutionforge.workflows.engine import Engine
from solutionforge.workflows.worker import Worker
from tests.helpers import Api
from tests.workflow_support import (
    InstrumentedSteps,
    SimulatedCrash,
    get_exec,
    make_workflow,
    start,
    step,
)


def linear(*steps: dict, **kw: object) -> dict:  # type: ignore[type-arg]
    chained = [dict(s) for s in steps]
    for a, b in itertools.pairwise(chained):
        a.setdefault("next", b["id"])
    return {"start": chained[0]["id"], "steps": chained, **kw}


async def _expire_lease(db: AsyncSession, execution_id: str) -> None:
    await db.execute(
        sa.update(Execution)
        .where(Execution.id == uuid.UUID(execution_id))
        .values(lease_expires_at=utcnow() - timedelta(seconds=1))
    )
    await db.commit()


# ------------------------------------------------------------------ happy paths


async def test_linear_workflow_with_state_and_output(api: Api, engine: Engine) -> None:
    s = await make_workflow(
        api,
        linear(
            step("greet", "transform", {"set": {"msg": "Hello {{ $.input.name }}"}}),
            step("shout", "transform", {"output": {"loud": "{{ $.state.msg }}!"}}),
            inputs={"name": {"type": "string"}},
            output={"message": "$.steps.shout.loud"},
        ),
    )
    eid = await start(api, s, {"name": "Ada"})
    assert (await get_exec(api, s, eid))["status"] == "queued"

    assert await engine.run_until_idle() == 1
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded"
    assert ex["output"] == {"message": "Hello Ada!"}
    assert ex["state"] == {"msg": "Hello Ada"}
    assert [(st["step_id"], st["status"], st["attempt"]) for st in ex["steps"]] == [
        ("greet", "succeeded", 1),
        ("shout", "succeeded", 1),
    ]
    assert ex["steps_used"] == 2 and ex["finished_at"]


async def test_conditional_branching(api: Api, engine: Engine) -> None:
    definition = {
        "start": "route",
        "inputs": {"amount": {"type": "number"}},
        "steps": [
            step(
                "route",
                "condition",
                {
                    "branches": [
                        {
                            "when": {"path": "$.input.amount", "op": "gt", "value": 1000},
                            "goto": "manual",
                        }
                    ],
                    "default": "auto",
                },
            ),
            step("manual", "transform", {"set": {"path": "manual_review"}}),
            step("auto", "transform", {"set": {"path": "auto_approved"}}),
        ],
        "output": {"decision": "$.state.path"},
    }
    s = await make_workflow(api, definition)
    big, small = await start(api, s, {"amount": 5000}), await start(api, s, {"amount": 10})
    await engine.run_until_idle()
    assert (await get_exec(api, s, big))["output"] == {"decision": "manual_review"}
    assert (await get_exec(api, s, small))["output"] == {"decision": "auto_approved"}


# ------------------------------------------------------------------ bounded execution


async def test_infinite_loop_is_stopped_by_step_budget(api: Api, engine: Engine) -> None:
    definition = {
        "start": "a",
        "limits": {"max_steps": 10},
        "steps": [
            step("a", "transform", {"set": {"x": 1}}, next="b"),
            step(
                "b",
                "condition",
                {
                    "branches": [
                        {"when": {"path": "$.state.x", "op": "eq", "value": 1}, "goto": "a"}
                    ],
                    "default": None,
                },
            ),
        ],
    }
    s = await make_workflow(api, definition)
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "budget_exceeded"
    assert ex["error"]["code"] == "max_steps_exceeded"
    assert ex["steps_used"] == 10 and len(ex["steps"]) == 10


async def test_active_time_budget(api: Api, engine: Engine) -> None:
    definition = {
        "start": "nap",
        "limits": {"max_active_seconds": 1},
        "steps": [
            step("nap", "sleep", {"seconds": 0.4}, next="again"),
            step(
                "again",
                "condition",
                {
                    "branches": [
                        {
                            "when": {"path": "$.steps.nap.slept", "op": "gt", "value": 0},
                            "goto": "nap",
                        }
                    ],
                    "default": None,
                },
            ),
        ],
    }
    s = await make_workflow(api, definition)
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "budget_exceeded"
    assert ex["error"]["code"] == "max_active_time_exceeded"
    assert ex["active_ms"] >= 1000


async def test_step_timeout_then_failure(api: Api, engine: Engine) -> None:
    s = await make_workflow(
        api,
        linear(
            step(
                "slow",
                "sleep",
                {"seconds": 5},
                timeout_seconds=0.1,
                retry={"max_attempts": 2, "initial_backoff_seconds": 0},
            )
        ),
    )
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed"
    assert ex["error"]["code"] == "timeout" and ex["error"]["attempt"] == 2
    assert [st["status"] for st in ex["steps"]] == ["failed", "failed"]


# ------------------------------------------------------------------ retries and fallbacks


async def test_transient_failure_is_retried(
    api: Api, engine: Engine, test_steps: InstrumentedSteps
) -> None:
    s = await make_workflow(
        api,
        linear(
            step(
                "call",
                "flaky",
                {"fail_times": 2},
                retry={"max_attempts": 3, "initial_backoff_seconds": 0},
            )
        ),
    )
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded"
    assert [(st["attempt"], st["status"]) for st in ex["steps"]] == [
        (1, "failed"),
        (2, "failed"),
        (3, "succeeded"),
    ]
    assert ex["steps"][0]["error"]["code"] == "upstream"


async def test_retry_waits_for_backoff(api: Api, engine: Engine, db: AsyncSession) -> None:
    s = await make_workflow(
        api,
        linear(
            step(
                "call",
                "flaky",
                {"fail_times": 1},
                retry={"max_attempts": 2, "initial_backoff_seconds": 60},
            )
        ),
    )
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "queued"  # scheduled, not busy-looping
    assert await engine.claim() is None  # not due yet

    await db.execute(sa.update(Execution).values(run_after=utcnow()))
    await db.commit()
    await engine.run_until_idle()
    assert (await get_exec(api, s, eid))["status"] == "succeeded"


async def test_non_retryable_failure_is_not_retried(api: Api, engine: Engine) -> None:
    s = await make_workflow(
        api,
        linear(
            step(
                "call",
                "flaky",
                {"fail_times": 1, "retryable": False},
                retry={"max_attempts": 5, "initial_backoff_seconds": 0},
            )
        ),
    )
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed" and len(ex["steps"]) == 1


async def test_on_error_routes_to_fallback_with_error_in_scope(api: Api, engine: Engine) -> None:
    definition = {
        "start": "primary",
        "steps": [
            step(
                "primary",
                "flaky",
                {"fail_times": 9},
                on_error="fallback",
                next="done",
                retry={"max_attempts": 2, "initial_backoff_seconds": 0},
            ),
            step(
                "fallback",
                "transform",
                {"set": {"degraded": True, "reason": "$.steps.primary.error.code"}},
                next="done",
            ),
            step("done", "transform"),
        ],
        "output": {"degraded": "$.state.degraded", "reason": "$.state.reason"},
    }
    s = await make_workflow(api, definition)
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded"
    assert ex["output"] == {"degraded": True, "reason": "upstream"}
    assert [st["step_id"] for st in ex["steps"]] == ["primary", "primary", "fallback", "done"]


async def test_unhandled_handler_exception_is_contained(api: Api, engine: Engine) -> None:
    s = await make_workflow(api, linear(step("x", "boom")))
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed"
    assert ex["error"]["code"] == "unhandled_error"
    assert "hunter2" not in str(ex)  # exception text never reaches API responses


async def test_oversized_step_output_fails_cleanly(api: Api, engine: Engine) -> None:
    s = await make_workflow(api, linear(step("x", "big", {"size": 300_000})))
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed" and ex["error"]["code"] == "output_too_large"


async def test_handler_cannot_mutate_persisted_state(api: Api, engine: Engine) -> None:
    s = await make_workflow(
        api, linear(step("m", "mutate_scope"), output={"state_keys": "$.state"})
    )
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded"
    assert ex["state"] == {}


# ------------------------------------------------------------------ durability


async def test_crash_mid_step_resumes_from_last_checkpoint(
    api: Api,
    app,
    engine: Engine,
    test_steps: InstrumentedSteps,
    db: AsyncSession,  # type: ignore[no-untyped-def]
) -> None:
    s = await make_workflow(
        api,
        linear(
            step("before", "count", {"name": "before"}),
            step(
                "risky",
                "crash",
                {"crash_times": 1},
                retry={"max_attempts": 3, "initial_backoff_seconds": 0},
            ),
            step("after", "count", {"name": "after"}),
        ),
    )
    eid = await start(api, s)

    with pytest.raises(SimulatedCrash):  # worker-1 dies inside "risky"
        await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "running" and ex["current_step"] == "risky"
    assert [st["step_id"] for st in ex["steps"]] == ["before"]  # checkpoint survived

    # Nobody can take over until the lease expires.
    worker2 = Engine(app.state.sessionmaker, app.state.step_registry, worker_id="worker-2")
    assert await worker2.claim() is None
    await _expire_lease(db, eid)
    await worker2.run_until_idle()

    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded"
    assert test_steps.count.calls == {"before": 1, "after": 1}  # completed steps not re-run
    risky = [st for st in ex["steps"] if st["step_id"] == "risky"]
    assert [(st["attempt"], st["status"]) for st in risky] == [(2, "succeeded")]
    assert risky[0]["output"] == {"recovered_on_attempt": 2}


async def test_poison_pill_step_exhausts_attempts(
    api: Api,
    app,
    engine: Engine,
    db: AsyncSession,  # type: ignore[no-untyped-def]
) -> None:
    """A step that kills its worker every time must not crash-loop the fleet forever."""
    s = await make_workflow(
        api, linear(step("risky", "crash", {"crash_times": 99}, retry={"max_attempts": 2}))
    )
    eid = await start(api, s)
    for n in range(2):
        worker = Engine(app.state.sessionmaker, app.state.step_registry, worker_id=f"w{n}")
        with pytest.raises(SimulatedCrash):
            await worker.run_until_idle()
        await _expire_lease(db, eid)

    final = Engine(app.state.sessionmaker, app.state.step_registry, worker_id="w-final")
    await final.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed"
    assert ex["error"]["code"] == "attempts_exhausted"


async def test_zombie_worker_cannot_overwrite_after_losing_lease(
    api: Api, engine: Engine, db: AsyncSession
) -> None:
    s = await make_workflow(api, linear(step("s", "steal"), step("t", "transform")))
    await start(api, s)
    await engine.run_until_idle()

    ex = (await db.scalars(sa.select(Execution))).one()
    assert ex.lease_owner == "thief"
    assert ex.current_step == "s" and ex.step_outputs == {}  # zombie's result discarded
    steps = (await db.scalars(sa.select(ExecutionStep))).all()
    assert steps == []


async def test_only_one_worker_claims_an_execution(api: Api, app) -> None:  # type: ignore[no-untyped-def]
    s = await make_workflow(api, linear(step("a", "transform")))
    await start(api, s)
    engines = [
        Engine(app.state.sessionmaker, app.state.step_registry, worker_id=f"w{i}") for i in range(5)
    ]
    claims = await asyncio.gather(*(e.claim() for e in engines))
    assert sum(c is not None for c in claims) == 1


# ------------------------------------------------------------------ worker process


async def test_worker_loop_processes_and_shuts_down_gracefully(api: Api, engine: Engine) -> None:
    s = await make_workflow(
        api, linear(step("a", "sleep", {"seconds": 0.05}), step("b", "transform"))
    )
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(engine, concurrency=2, poll_interval=0.02).run(stop))
    ids = [await start(api, s) for _ in range(4)]

    async def all_done() -> bool:
        return all([(await get_exec(api, s, i))["status"] == "succeeded" for i in ids])

    for _ in range(200):
        if await all_done():
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert await all_done()


async def test_final_output_referencing_missing_data_fails_cleanly(
    api: Api, engine: Engine
) -> None:
    s = await make_workflow(
        api, linear(step("a", "transform", {"output": {"x": 1}}), output={"y": "$.steps.a.nope"})
    )
    eid = await start(api, s)
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed"
    assert ex["error"]["code"] == "output_resolution_error"


async def test_step_type_removed_after_version_created_fails_cleanly(
    api: Api,
    app,
    test_steps: InstrumentedSteps,  # type: ignore[no-untyped-def]
) -> None:
    """A deploy that drops a step type must fail affected executions, not crash workers."""
    from solutionforge.workflows.steps import default_registry

    s = await make_workflow(api, linear(step("a", "count", {"name": "x"})))
    eid = await start(api, s)
    old_worker = Engine(app.state.sessionmaker, default_registry(), worker_id="old-build")
    await old_worker.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed"
    assert ex["error"]["code"] == "definition_invalid"
