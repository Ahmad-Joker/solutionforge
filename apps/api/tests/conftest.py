"""Shared fixtures.

The schema is always created by running the real Alembic migrations, so tests exercise
the same DDL production uses. Backend selection:

- ``SF_TEST_DATABASE_URL`` set (CI, docker compose): PostgreSQL.
- otherwise: a temporary SQLite file (fast local loop, no services needed).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.connectors.simulated import SIMULATED_TOOLS
from solutionforge.core.config import Environment, Settings
from solutionforge.db.base import Base
from solutionforge.llm.providers.mock import MockProvider
from solutionforge.llm.service import LLMService, RetryConfig
from solutionforge.main import create_app
from solutionforge.tools.catalog import ToolCatalog
from solutionforge.tools.executor import ToolExecutor
from solutionforge.workflows.engine import Engine
from solutionforge.workflows.steps import default_registry
from tests.helpers import Api
from tests.tool_support import test_tools
from tests.workflow_support import (
    BigOutputStep,
    BoomStep,
    CountStep,
    CrashStep,
    FlakyStep,
    InstrumentedSteps,
    MutateScopeStep,
    SleepStep,
    StealStep,
)

API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    url = os.environ.get("SF_TEST_DATABASE_URL")
    if url is None:
        db_file = tmp_path_factory.mktemp("db") / "test.db"
        url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.downgrade(cfg, "base")  # clean slate on a reused Postgres database
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=database_url,
        jwt_secret="test-secret-that-is-at-least-32-characters-long",  # type: ignore[arg-type]
        log_json=False,
        log_level="WARNING",
        password_hash_profile="fast-insecure-test",
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    yield application
    await _truncate_all(application)
    await application.state.engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api(client: AsyncClient) -> Api:
    return Api(client)


@pytest.fixture
async def db(app: FastAPI) -> AsyncIterator[AsyncSession]:
    """Direct DB access for assertions the API does not expose."""
    async with app.state.sessionmaker() as session:
        yield session


async def _truncate_all(app: FastAPI) -> None:
    tables = [t.name for t in reversed(Base.metadata.sorted_tables)]
    async with app.state.engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # TRUNCATE bypasses the row-level append-only trigger on audit_events.
            await conn.execute(sa.text(f"TRUNCATE {', '.join(tables)} CASCADE"))
        else:
            for name in tables:
                await conn.execute(sa.text(f"DELETE FROM {name}"))  # noqa: S608 (static names)


@pytest.fixture
def tool_harness(app: FastAPI) -> ToolExecutor:
    """Executor over the real simulated tools plus fault-injection tools, no backoff sleeps."""

    async def no_sleep(_: float) -> None:
        return None

    real: ToolExecutor = app.state.tool_executor
    executor = ToolExecutor(
        ToolCatalog([*SIMULATED_TOOLS, *test_tools()]),
        app.state.sessionmaker,
        real.cipher,
        sleep=no_sleep,
    )
    app.state.tool_executor = executor
    return executor


@pytest.fixture
def test_steps(app: FastAPI, tool_harness: ToolExecutor) -> InstrumentedSteps:
    """Registers fault-injection step types on the app's registry (API + engine share it)."""
    registry = default_registry(app.state.llm_service, tool_harness, app.state.retriever)
    steps = InstrumentedSteps(CountStep(), FlakyStep(), CrashStep())
    for handler in (
        steps.count,
        steps.flaky,
        steps.crash,
        SleepStep(),
        BoomStep(),
        BigOutputStep(),
        StealStep(app.state.sessionmaker),
        MutateScopeStep(),
    ):
        registry.register(handler)
    app.state.step_registry = registry
    return steps


@pytest.fixture
def engine(app: FastAPI, test_steps: InstrumentedSteps) -> Engine:
    return Engine(app.state.sessionmaker, app.state.step_registry, worker_id="worker-1")


@pytest.fixture
def mock_llm(app: FastAPI) -> MockProvider:
    """The app's mock LLM provider, with backoff sleeps disabled for fast tests."""
    service: LLMService = app.state.llm_service
    service.retry = RetryConfig(backoff_base_seconds=0.0, backoff_max_seconds=0.0)
    provider = service.providers["mock"]
    assert isinstance(provider, MockProvider)
    return provider
