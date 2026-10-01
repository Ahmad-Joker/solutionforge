"""Fixed-window rate limiting for abuse-prone endpoints (login, refresh, registration).

Backends:
- ``RedisRateLimiter``: shared across API replicas (``INCR`` + ``EXPIRE`` in one pipeline).
- ``InMemoryRateLimiter``: per process; used when no Redis is configured (dev/tests). With
  several replicas each enforces its own window, so the effective limit multiplies; the
  config docs say so and production sets ``SF_REDIS_URL``.

Keys are hashed so raw emails/IPs never become Redis keys.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    max_requests: int
    window_seconds: int


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    retry_after_seconds: int
    remaining: int


class RateLimiter(Protocol):
    async def hit(self, limit: Limit, key: str) -> Decision: ...


def _key(limit: Limit, key: str, window: int) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"rl:{limit.name}:{window}:{digest}"


class InMemoryRateLimiter:
    MAX_KEYS = 100_000  # bounded memory: oldest windows are evicted first

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._counts: dict[str, int] = {}

    async def hit(self, limit: Limit, key: str) -> Decision:
        now = self._clock()
        window = int(now // limit.window_seconds)
        k = _key(limit, key, window)
        if len(self._counts) >= self.MAX_KEYS:
            self._evict(now)
        count = self._counts.get(k, 0) + 1
        self._counts[k] = count
        retry = int((window + 1) * limit.window_seconds - now) + 1
        return Decision(count <= limit.max_requests, retry, max(0, limit.max_requests - count))

    def _evict(self, now: float) -> None:
        for k in list(self._counts)[: len(self._counts) // 2]:
            del self._counts[k]


class RedisRateLimiter:
    def __init__(self, client: Any, clock: Callable[[], float] = time.time) -> None:
        self._redis = client
        self._clock = clock

    async def hit(self, limit: Limit, key: str) -> Decision:
        now = self._clock()
        window = int(now // limit.window_seconds)
        k = _key(limit, key, window)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(k)
            pipe.expire(k, limit.window_seconds + 1)
            count, _ = await pipe.execute()
        retry = int((window + 1) * limit.window_seconds - now) + 1
        return Decision(
            int(count) <= limit.max_requests, retry, max(0, limit.max_requests - int(count))
        )


class FailOpenRateLimiter:
    """Wraps a limiter so a Redis outage degrades to 'allow' (logged) instead of locking
    every user out. Login still requires the correct password; this trades brute-force
    resistance during an outage for availability, deliberately."""

    def __init__(self, inner: RateLimiter, on_error: Callable[[Exception], None]) -> None:
        self._inner = inner
        self._on_error = on_error

    async def hit(self, limit: Limit, key: str) -> Decision:
        try:
            return await self._inner.hit(limit, key)
        except Exception as exc:  # backend failure, never a limit decision
            self._on_error(exc)
            return Decision(True, 0, limit.max_requests)


LOGIN_PER_ACCOUNT = Limit("login_account", 10, 300)
LOGIN_PER_IP = Limit("login_ip", 50, 300)
REGISTER_PER_IP = Limit("register_ip", 20, 3600)
REFRESH_PER_IP = Limit("refresh_ip", 120, 60)
