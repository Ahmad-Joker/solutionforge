"""Fault-injection tools for executor tests (part of the testing strategy, not features)."""

from __future__ import annotations

import asyncio
from collections import Counter

from solutionforge.connectors.simulated import CreateTicket, CreateTicketIn, TicketOut
from solutionforge.security.rbac import Permission
from solutionforge.tools.spec import (
    RiskLevel,
    Tool,
    ToolContext,
    ToolModel,
    ToolSpec,
    ToolTransientError,
)
from tests.workflow_support import SimulatedCrash


class Ping(ToolModel):
    n: int = 0


class Pong(ToolModel):
    calls: int


class FlakyRead(Tool):
    spec = ToolSpec(
        name="test.flaky_read",
        description="fails twice",
        input_model=Ping,
        output_model=Pong,
        risk_level=RiskLevel.READ_ONLY,
        required_permission=Permission.WORKFLOW_EXECUTE,
        max_attempts=3,
        idempotent=True,
    )

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: Ping, ctx: ToolContext) -> Pong:
        self.calls += 1
        if self.calls <= 2:
            raise ToolTransientError("upstream 503")
        return Pong(calls=self.calls)


class FlakyWriteNoKey(Tool):
    """A write that cannot deduplicate: the executor must NOT retry it."""

    spec = ToolSpec(
        name="test.flaky_write",
        description="unsafe to repeat",
        input_model=Ping,
        output_model=Pong,
        risk_level=RiskLevel.LOW_RISK_WRITE,
        required_permission=Permission.WORKFLOW_EXECUTE,
        max_attempts=3,
    )

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: Ping, ctx: ToolContext) -> Pong:
        self.calls += 1
        raise ToolTransientError("timeout after the write may have happened")


class Slow(Tool):
    spec = ToolSpec(
        name="test.slow",
        description="hangs",
        input_model=Ping,
        output_model=Pong,
        risk_level=RiskLevel.READ_ONLY,
        required_permission=Permission.WORKFLOW_EXECUTE,
        timeout_seconds=0.05,
    )

    async def execute(self, args: Ping, ctx: ToolContext) -> Pong:
        await asyncio.sleep(5)
        return Pong(calls=1)


class Boom(Tool):
    spec = ToolSpec(
        name="test.boom",
        description="bug",
        input_model=Ping,
        output_model=Pong,
        risk_level=RiskLevel.READ_ONLY,
        required_permission=Permission.WORKFLOW_EXECUTE,
    )

    async def execute(self, args: Ping, ctx: ToolContext) -> Pong:
        raise RuntimeError("password=hunter2 leaked in a stack trace")


class BadOutput(Tool):
    spec = ToolSpec(
        name="test.bad_output",
        description="wrong type",
        input_model=Ping,
        output_model=Pong,
        risk_level=RiskLevel.READ_ONLY,
        required_permission=Permission.WORKFLOW_EXECUTE,
    )

    async def execute(self, args: Ping, ctx: ToolContext) -> Pong:
        return Ping(n=1)  # type: ignore[return-value]


class CrashAfterTicket(Tool):
    """Creates the ticket, then the worker dies before checkpointing (first time only)."""

    spec = ToolSpec(
        name="test.crash_after_ticket",
        description="side effect then crash",
        input_model=CreateTicketIn,
        output_model=TicketOut,
        risk_level=RiskLevel.LOW_RISK_WRITE,
        required_permission=Permission.WORKFLOW_EXECUTE,
        supports_idempotency_key=True,
    )

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self._inner = CreateTicket()

    async def execute(self, args: CreateTicketIn, ctx: ToolContext) -> TicketOut:
        out = await self._inner.execute(args, ctx)
        self.calls[ctx.idempotency_key or ""] += 1
        if self.calls[ctx.idempotency_key or ""] == 1:
            raise SimulatedCrash
        return out


def test_tools() -> list[Tool]:
    return [FlakyRead(), FlakyWriteNoKey(), Slow(), Boom(), BadOutput(), CrashAfterTicket()]


test_tools.__test__ = False  # type: ignore[attr-defined]
