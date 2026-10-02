"""Prometheus scrape endpoint (unversioned).

Protected by a bearer token (``SF_METRICS_TOKEN``). Without a token it is served only in
dev/test; in staging/production it answers 404 so it is never accidentally public.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST

from solutionforge.core.config import Environment, Settings
from solutionforge.observability.metrics import render

router = APIRouter(tags=["observability"], include_in_schema=False)


def _authorized(request: Request, settings: Settings) -> bool:
    token = settings.metrics_token
    if token is None:
        return settings.environment in (Environment.DEV, Environment.TEST)
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {token.get_secret_value()}"
    return hmac.compare_digest(supplied.encode(), expected.encode())


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    if not _authorized(request, request.app.state.settings):
        return Response(status_code=404)
    return Response(render(), media_type=CONTENT_TYPE_LATEST)
