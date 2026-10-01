"""Application factory."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from solutionforge import __version__
from solutionforge.api import health
from solutionforge.api.errors import install_error_handlers
from solutionforge.api.middleware import RequestContextMiddleware
from solutionforge.api.v1 import approvals, audit, auth, knowledge, orgs, tools, usage, workflows
from solutionforge.background import run_background
from solutionforge.core.config import Settings, get_settings
from solutionforge.core.logging import configure_logging, get_logger
from solutionforge.db.session import build_engine, build_sessionmaker
from solutionforge.llm.factory import build_llm_service
from solutionforge.retrieval.factory import build_retriever
from solutionforge.security import passwords
from solutionforge.security.ratelimit import (
    FailOpenRateLimiter,
    InMemoryRateLimiter,
    RateLimiter,
    RedisRateLimiter,
)
from solutionforge.tools.factory import build_tool_executor
from solutionforge.workflows.steps import default_registry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    passwords.configure(settings.password_hash_profile)

    # Engines connect lazily, so building one here is cheap and keeps the app usable
    # by test clients that do not run the ASGI lifespan.
    engine = build_engine(settings.database_url, echo=settings.database_echo)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        background: asyncio.Task[None] | None = None
        if settings.embedded_worker:
            # Single-process mode (demos, E2E): run the worker loops alongside the API.
            background = asyncio.create_task(
                run_background(
                    stop,
                    settings,
                    app.state.sessionmaker,
                    app.state.step_registry,
                    app.state.retriever,
                )
            )
        yield
        stop.set()
        if background is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(background, timeout=30)
        await engine.dispose()

    app = FastAPI(
        title="SolutionForge API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = build_sessionmaker(engine)
    app.state.llm_service = build_llm_service(settings, app.state.sessionmaker)
    app.state.tool_executor = build_tool_executor(settings, app.state.sessionmaker)
    app.state.retriever = build_retriever(app.state.sessionmaker)
    app.state.step_registry = default_registry(
        app.state.llm_service, app.state.tool_executor, app.state.retriever
    )

    app.state.rate_limiter = build_rate_limiter(settings)

    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(auth.router)
    v1.include_router(orgs.router)
    v1.include_router(audit.router)
    v1.include_router(workflows.router)
    v1.include_router(usage.router)
    v1.include_router(tools.router)
    v1.include_router(knowledge.router)
    v1.include_router(approvals.router)
    app.include_router(v1)
    app.include_router(health.router)
    return app


def build_rate_limiter(settings: Settings) -> RateLimiter:
    if settings.redis_url is None:
        return InMemoryRateLimiter()
    import redis.asyncio as redis

    log = get_logger("solutionforge.ratelimit")
    client = redis.from_url(settings.redis_url.get_secret_value(), socket_timeout=0.5)
    return FailOpenRateLimiter(
        RedisRateLimiter(client),
        on_error=lambda exc: log.warning("rate_limiter_unavailable", error=type(exc).__name__),
    )


def app_factory() -> FastAPI:  # uvicorn --factory solutionforge.main:app_factory
    return create_app()
