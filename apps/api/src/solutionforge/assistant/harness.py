"""An in-process environment for evals and benchmarks: the real app on a throwaway
database, a demo organization, knowledge bases built from the demo corpus, demo tickets.

Everything goes through the same code paths as production (API for accounts and knowledge
bases, ingestion worker, retriever, assistant service). Only the database is local.
"""

from __future__ import annotations

import asyncio
import secrets
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from solutionforge.assistant.corpus import load_corpus, seed_tickets, sync_documents
from solutionforge.assistant.service import AssistantService
from solutionforge.core.config import Environment, Settings
from solutionforge.db.tenancy import TenantContext
from solutionforge.retrieval.ingest import IngestionWorker
from solutionforge.security.rbac import Role

API_ROOT = Path(__file__).absolute().parents[3]  # absolute(), not resolve(): keep symlinked paths


@dataclass(frozen=True)
class KBSpec:
    label: str
    chunk_size: int = 800
    chunk_overlap: int = 120
    embedder: str = "hashing-v1"
    include_injection: bool = False


@dataclass
class Env:
    app: FastAPI
    settings: Settings
    ctx: TenantContext
    kbs: dict[str, uuid.UUID]
    database_url: str

    @property
    def assistant(self) -> AssistantService:
        svc: AssistantService = self.app.state.assistant
        return svc

    async def close(self) -> None:
        await self.app.state.engine.dispose()


def migrate(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


async def build_env(
    kb_specs: list[KBSpec],
    *,
    database_url: str | None = None,
    anthropic_api_key: str | None = None,
    embedding_cache_dir: str | None = None,
) -> Env:
    if database_url is None:
        db_file = Path(tempfile.mkdtemp(prefix="sf-eval-")) / "eval.db"
        database_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    # Alembic's async env calls asyncio.run(), so migrate off this event loop.
    await asyncio.to_thread(migrate, database_url)
    semantic = any(s.embedder != "hashing-v1" for s in kb_specs)
    settings = Settings(
        environment=Environment.TEST,
        database_url=database_url,
        jwt_secret=secrets.token_urlsafe(48),
        log_json=False,
        log_level="WARNING",
        password_hash_profile="fast-insecure-test",  # noqa: S106 - throwaway eval accounts
        rate_limit_enabled=False,
        semantic_embedder_enabled=semantic,
        embedding_cache_dir=embedding_cache_dir,
        anthropic_api_key=anthropic_api_key,
    )
    from solutionforge.main import create_app

    app = create_app(settings)
    transport = ASGITransport(app=app, raise_app_exceptions=True)
    async with AsyncClient(transport=transport, base_url="http://eval") as c:
        email = f"eval-{uuid.uuid4().hex[:8]}@example.com"
        password = secrets.token_urlsafe(16)
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": "eval"},
        )
        r.raise_for_status()
        user_id = r.json()["id"]
        token = (
            await c.post("/api/v1/auth/login", json={"email": email, "password": password})
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        r = await c.post("/api/v1/orgs", json={"name": "Harbor & Pine (demo)"}, headers=headers)
        r.raise_for_status()
        org_id = r.json()["id"]
        ctx = TenantContext(uuid.UUID(org_id), uuid.UUID(user_id), Role.OWNER)
        kbs: dict[str, uuid.UUID] = {}
        for spec in kb_specs:
            r = await c.post(
                f"/api/v1/orgs/{org_id}/knowledge-bases",
                json={
                    "name": f"Harbor & Pine — {spec.label}",
                    "chunk_size": spec.chunk_size,
                    "chunk_overlap": spec.chunk_overlap,
                    "embedder": spec.embedder,
                },
                headers=headers,
            )
            r.raise_for_status()
            kbs[spec.label] = uuid.UUID(r.json()["id"])
            await sync_documents(
                app.state.sessionmaker,
                ctx,
                kbs[spec.label],
                load_corpus(include_injection=spec.include_injection),
            )
    worker = IngestionWorker(app.state.sessionmaker, app.state.retriever.embedders)
    await worker.run_until_idle()
    await seed_tickets(app.state.sessionmaker, ctx.organization_id)
    return Env(app, settings, ctx, kbs, database_url)
