"""Request-ID propagation and access logging (pure ASGI, so it also wraps error responses)."""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from solutionforge.core.logging import get_logger
from solutionforge.observability import metrics

log = get_logger("solutionforge.access")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        # Accept a caller-supplied ID only if it is safe to log; otherwise mint one.
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status = 500
        start = time.perf_counter()
        method = scope["method"]

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # The HTTP server span itself comes from FastAPI's built-in OpenTelemetry
            # support (route template, query redaction, W3C propagation).
            elapsed = time.perf_counter() - start
            route = route_template(scope)
            metrics.HTTP_REQUESTS.labels(method, route, str(status)).inc()
            metrics.HTTP_DURATION.labels(method, route).observe(elapsed)
            log.info(
                "http_request",
                method=method,
                path=scope["path"],
                route=route,
                status=status,
                duration_ms=round(elapsed * 1000, 2),
            )
            structlog.contextvars.unbind_contextvars("request_id")


def route_template(scope: Scope) -> str:
    """The matched route's full path template (a bounded label), never the raw path.

    FastAPI keeps included routers nested, so ``scope["route"].path`` lacks the static
    prefixes of outer routers (``/api/v1``). Those are exactly the leading request-path
    segments the route template does not cover (no route uses multi-segment parameters).
    """
    path = getattr(scope.get("route"), "path", None)
    if not isinstance(path, str):
        return "unmatched"
    tail = path.strip("/").split("/") if path.strip("/") else []
    segments = scope["path"].strip("/").split("/")
    prefix = segments[: len(segments) - len(tail)]
    return "/" + "/".join([*prefix, *tail]) if prefix else path
