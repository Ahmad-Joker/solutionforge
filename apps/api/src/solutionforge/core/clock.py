"""Single source of 'now' so time-dependent logic (expiry, audit timestamps) is testable."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
