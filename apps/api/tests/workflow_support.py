"""Instrumented step types and helpers for engine tests.

These are part of the testing strategy (fault injection), not substitutes for real features:
they let tests deterministically produce timeouts, transient failures, worker crashes and
lease theft.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.domain.workflow import Execution
from solutionforge.workflows.registry import StepContext, StepError, StepHandler, StepResult
from tests.helpers import Api, Session


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CountCfg(_Cfg):
    name: str


class CountStep(StepHandler[CountCfg]):
    """Records how many times it actually ran (per name). Proves steps are not re-run."""

    type = "count"
    config_model = CountCfg

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    async def run(self, config: CountCfg, ctx: StepContext) -> StepResult:
        self.calls[config.name] += 1
        return StepResult(output={"n": self.calls[config.name]})


class FlakyCfg(_Cfg):
    fail_times: int
    retryable: bool = True


class FlakyStep(StepHandler[FlakyCfg]):
    """Fails the first ``fail_times`` calls per step visit (keyed by idempotency key)."""

    type = "flaky"
    config_model = FlakyCfg

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    async def run(self, config: FlakyCfg, ctx: StepContext) -> StepResult:
        self.calls[ctx.idempotency_key] += 1
        if self.calls[ctx.idempotency_key] <= config.fail_times:
            raise StepError("transient upstream error", retryable=config.retryable, code="upstream")
        return StepResult(output={"attempt": ctx.attempt})


class SleepCfg(_Cfg):
    seconds: float


class SleepStep(StepHandler[SleepCfg]):
    type = "sleep"
    config_model = SleepCfg

    async def run(self, config: SleepCfg, ctx: StepContext) -> StepResult:
        await asyncio.sleep(config.seconds)
        return StepResult(output={"slept": config.seconds})


class SimulatedCrash(BaseException):
    """Stands in for SIGKILL / OOM: the worker vanishes mid-step without checkpointing."""


class CrashCfg(_Cfg):
    crash_times: int = 1


class CrashStep(StepHandler[CrashCfg]):
    type = "crash"
    config_model = CrashCfg

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    async def run(self, config: CrashCfg, ctx: StepContext) -> StepResult:
        self.calls[ctx.idempotency_key] += 1
        if self.calls[ctx.idempotency_key] <= config.crash_times:
            raise SimulatedCrash
        return StepResult(output={"recovered_on_attempt": ctx.attempt})


class BoomStep(StepHandler[_Cfg]):
    type = "boom"
    config_model = _Cfg

    async def run(self, config: _Cfg, ctx: StepContext) -> StepResult:
        raise RuntimeError("internal detail: db password is hunter2")


class BigCfg(_Cfg):
    size: int


class BigOutputStep(StepHandler[BigCfg]):
    type = "big"
    config_model = BigCfg

    async def run(self, config: BigCfg, ctx: StepContext) -> StepResult:
        return StepResult(output={"blob": "x" * config.size})


class StealStep(StepHandler[_Cfg]):
    """While running, another worker takes the lease (simulates a GC pause past expiry)."""

    type = "steal"
    config_model = _Cfg

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def run(self, config: _Cfg, ctx: StepContext) -> StepResult:
        async with self.sessionmaker() as s, s.begin():
            await s.execute(
                sa.update(Execution)
                .where(Execution.id == ctx.execution_id)
                .values(lease_owner="thief")
            )
        return StepResult(output={"should": "never be persisted"})


class MutateScopeStep(StepHandler[_Cfg]):
    type = "mutate_scope"
    config_model = _Cfg

    async def run(self, config: _Cfg, ctx: StepContext) -> StepResult:
        ctx.scope["state"]["injected"] = "by a buggy handler"
        return StepResult()


@dataclass
class InstrumentedSteps:
    count: CountStep
    flaky: FlakyStep
    crash: CrashStep


@dataclass
class WorkflowSetup:
    owner: Session
    org: str
    workflow: str

    def url(self, path: str = "") -> str:
        return f"/api/v1/orgs/{self.org}{path}"


async def make_workflow(
    api: Api, definition: dict[str, Any], *, deploy: bool = True, name: str = "wf"
) -> WorkflowSetup:
    owner = await api.user()
    org = await api.org(owner)
    r = await api.c.post(
        f"/api/v1/orgs/{org}/workflows", json={"name": name}, headers=owner.headers
    )
    assert r.status_code == 201, r.text
    wf = r.json()["id"]
    r = await api.c.post(
        f"/api/v1/orgs/{org}/workflows/{wf}/versions",
        json={"definition": definition},
        headers=owner.headers,
    )
    assert r.status_code == 201, r.text
    if deploy:
        r = await api.c.post(
            f"/api/v1/orgs/{org}/workflows/{wf}/deployments",
            json={"version": 1},
            headers=owner.headers,
        )
        assert r.status_code == 201, r.text
    return WorkflowSetup(owner, org, wf)


async def start(api: Api, s: WorkflowSetup, input: dict[str, Any] | None = None) -> str:
    r = await api.c.post(
        s.url(f"/workflows/{s.workflow}/executions"),
        json={"input": input or {}},
        headers=s.owner.headers,
    )
    assert r.status_code == 202, r.text
    return str(r.json()["id"])


async def get_exec(api: Api, s: WorkflowSetup, execution_id: str) -> dict[str, Any]:
    r = await api.c.get(s.url(f"/executions/{execution_id}"), headers=s.owner.headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def step(id_: str, type_: str, config: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return {"id": id_, "type": type_, "config": config or {}, **kw}
