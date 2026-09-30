"""Per-model circuit breaker (closed → open → half-open → closed).

State is per process. With several workers each keeps its own view, which is acceptable:
the goal is to stop *this* worker hammering a failing upstream, not global coordination.
A shared (Redis) breaker is a later optimisation.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class _Circuit:
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    trial_in_flight: bool = False


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._circuits: dict[str, _Circuit] = {}

    def state(self, key: str) -> BreakerState:
        c = self._circuits.get(key)
        if c is None:
            return BreakerState.CLOSED
        if c.state == BreakerState.OPEN and self._clock() - c.opened_at >= self.recovery_seconds:
            c.state = BreakerState.HALF_OPEN
            c.trial_in_flight = False
        return c.state

    def allow(self, key: str) -> bool:
        """Whether a call may proceed. In half-open, exactly one trial call is let through."""
        state = self.state(key)
        if state == BreakerState.CLOSED:
            return True
        if state == BreakerState.OPEN:
            return False
        c = self._circuits[key]
        if c.trial_in_flight:
            return False
        c.trial_in_flight = True
        return True

    def record_success(self, key: str) -> None:
        self._circuits[key] = _Circuit()

    def record_failure(self, key: str) -> None:
        c = self._circuits.setdefault(key, _Circuit())
        c.consecutive_failures += 1
        if c.state == BreakerState.HALF_OPEN or c.consecutive_failures >= self.failure_threshold:
            c.state = BreakerState.OPEN
            c.opened_at = self._clock()
            c.trial_in_flight = False
