"""Liveness and readiness probes (unversioned, unauthenticated)."""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    """The process is up. Deliberately checks nothing external."""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(request: Request) -> JSONResponse:
    """Ready to take traffic: dependencies reachable within a short timeout."""
    checks: dict[str, str] = {}
    try:
        async with asyncio.timeout(2):
            async with request.app.state.engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # any failure means "not ready"; details go to logs, not the probe
        checks["database"] = "unavailable"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "checks": checks},
    )
