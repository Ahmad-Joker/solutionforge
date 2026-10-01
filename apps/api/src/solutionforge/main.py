"""Application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from solutionforge import __version__
from solutionforge.api import health
from solutionforge.api.errors import install_error_handlers
from solutionforge.api.middleware import RequestContextMiddleware
from solutionforge.api.v1 import audit, auth, knowledge, orgs, tools, usage, workflows
from solutionforge.core.config import Settings, get_settings
from solutionforge.core.logging import configure_logging
from solutionforge.db.session import build_engine, build_sessionmaker
from solutionforge.llm.factory import build_llm_service
from solutionforge.retrieval.factory import build_retriever
from solutionforge.tools.factory import build_tool_executor
from solutionforge.workflows.steps import default_registry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.log_json)

    # Engines connect lazily, so building one here is cheap and keeps the app usable
    # by test clients that do not run the ASGI lifespan.
    engine = build_engine(settings.database_url, echo=settings.database_echo)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
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
    app.include_router(v1)
    app.include_router(health.router)
    return app


def app_factory() -> FastAPI:  # uvicorn --factory solutionforge.main:app_factory
    return create_app()
