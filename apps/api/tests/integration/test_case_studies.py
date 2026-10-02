"""The three customer case studies (cases/*) run end to end on every CI build.

They use the deterministic mock model, so they verify the *platform* behaviour each case
study claims (routing, policy, approvals, redaction, retrieval, refusals), not model
quality. Thresholds are the measured baselines recorded in cases/*/README.md.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.domain import Approval, SimRefund, SimTicket
from solutionforge.retrieval.ingest import IngestionWorker
from solutionforge.services.eval_service import finalize_due_runs
from solutionforge.workflows.engine import Engine
from tests.helpers import Api

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "case_study.py"
_spec = importlib.util.spec_from_file_location("case_study", _SCRIPT)
assert _spec and _spec.loader
case_study = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(case_study)


# Lowest measured across backends (2026-10-02): 28/28 on SQLite/BM25, 27/28 on PostgreSQL
# full-text search ("do you take PayPal" ranks another article first). See cases/research.
BASELINE_RESEARCH_PASS_RATE = 0.96


async def _run(
    name: str,
    api: Api,
    client: AsyncClient,
    app: FastAPI,
    engine: Engine,
    ingestor: IngestionWorker,
) -> dict[str, Any]:
    owner = await api.user()
    org = await api.org(owner, f"Case {name}")

    async def drain() -> None:
        await ingestor.run_until_idle()
        await engine.run_until_idle()
        await finalize_due_runs(app.state.sessionmaker)

    return await case_study.run_case_study(  # type: ignore[no-any-return]
        client, owner.headers, f"/api/v1/orgs/{org}", name, drain=drain, wait_seconds=60
    )


def _failed(detail: dict[str, Any]) -> list[str]:
    return [
        f"{detail['case_names'][r['case_id']]}: {r['failures']}"
        for r in detail["results"]
        if not r["passed"]
    ]


async def test_support_case_study(
    api: Api,
    client: AsyncClient,
    app: FastAPI,
    engine: Engine,
    ingestor: IngestionWorker,
    db: AsyncSession,
) -> None:
    detail = await _run("support", api, client, app, engine, ingestor)
    assert detail["metrics"]["pass_rate"] == 1.0, _failed(detail)
    assert detail["metrics"]["security_cases_passed"] == detail["metrics"]["security_cases"] == 2
    # Credits never executed without a human: the requests were cancelled with the run.
    assert await db.scalar(sa.select(sa.func.count()).select_from(SimRefund)) == 0
    statuses = set(
        (
            await db.scalars(
                sa.select(Approval.status).where(Approval.tool_name == "payments.issue_refund")
            )
        ).all()
    )
    assert statuses == {"cancelled"}
    # PII never reached the ticketing system.
    bodies = " ".join((await db.scalars(sa.select(SimTicket.body))).all())
    assert "jane.doe@example.com" not in bodies and "4111 1111" not in bodies
    assert "[EMAIL]" in bodies and "[CARD]" in bodies and "[PHONE]" in bodies


async def test_ops_case_study(
    api: Api,
    client: AsyncClient,
    app: FastAPI,
    engine: Engine,
    ingestor: IngestionWorker,
) -> None:
    detail = await _run("ops", api, client, app, engine, ingestor)
    assert detail["metrics"]["pass_rate"] == 1.0, _failed(detail)


async def test_research_case_study(
    api: Api,
    client: AsyncClient,
    app: FastAPI,
    engine: Engine,
    ingestor: IngestionWorker,
) -> None:
    detail = await _run("research", api, client, app, engine, ingestor)
    failed = _failed(detail)
    # Retrieval is measured, not tuned to pass: baseline from cases/research/README.md.
    assert detail["metrics"]["pass_rate"] >= BASELINE_RESEARCH_PASS_RATE, failed
    refusal_and_security = [f for f in failed if "off-topic" in f or "injection" in f]
    assert refusal_and_security == []
