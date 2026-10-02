"""Maps exceptions to a single JSON error envelope:

{"error": {"code": str, "message": str, "details": {...}, "request_id": str | null}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

from solutionforge.core.errors import DomainError, TooManyRequests
from solutionforge.core.logging import get_logger
from solutionforge.tools.spec import ToolError, ToolNotFound

log = get_logger(__name__)


def _envelope(
    request: Request,
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    headers = dict(headers or {})
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("domain_error", code=exc.code, message=exc.message)
        headers = (
            {"Retry-After": str(exc.retry_after_seconds)}
            if isinstance(exc, TooManyRequests)
            else None
        )
        return _envelope(request, exc.status_code, exc.code, exc.message, exc.details, headers)

    @app.exception_handler(ToolError)
    async def _tool(request: Request, exc: ToolError) -> JSONResponse:
        status = 404 if isinstance(exc, ToolNotFound) else 422
        return _envelope(request, status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Strip "input" so rejected payloads (which may contain passwords) are not echoed.
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return _envelope(
            request, 422, "validation_failed", "Request validation failed", {"errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _envelope(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(PoolTimeout)
    async def _busy(request: Request, exc: PoolTimeout) -> JSONResponse:
        # Load shedding: every database connection is busy. Clients should retry shortly.
        log.warning("db_pool_exhausted", path=request.url.path)
        return _envelope(
            request,
            503,
            "server_busy",
            "The server is busy; please retry shortly",
            None,
            {"Retry-After": "1"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path)
        return _envelope(request, 500, "internal_error", "An internal error occurred")
