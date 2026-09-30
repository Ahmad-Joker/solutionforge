"""Usage ledger and budgets: tenant isolation, RBAC, secret handling, data minimisation."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from solutionforge.core.config import Environment, Settings
from solutionforge.domain import UsageRecord
from solutionforge.llm.providers.mock import MockProvider
from solutionforge.workflows.engine import Engine
from tests.helpers import Api
from tests.workflow_support import make_workflow, start, step

pytestmark = pytest.mark.security

LLM_WF = {
    "start": "a",
    "steps": [step("a", "llm", {"model": "mock:mock-1", "prompt": "secret customer note: 4111"})],
}


async def test_usage_and_budget_are_tenant_isolated(
    api: Api, client: AsyncClient, engine: Engine, mock_llm: MockProvider
) -> None:
    victim = await make_workflow(api, LLM_WF, name="victim-wf")
    victim_exec = await start(api, victim)
    await engine.run_until_idle()
    attacker = await api.user()
    attacker_org = await api.org(attacker, "Evil Corp")

    for method, path, body in [
        ("GET", "/budget", None),
        ("PUT", "/budget", {"daily_limit_usd": "0"}),
        ("GET", "/usage/summary", None),
        ("GET", "/usage/records", None),
    ]:
        r = await client.request(
            method, f"/api/v1/orgs/{victim.org}{path}", json=body, headers=attacker.headers
        )
        assert r.status_code == 404, path

    # Confused deputy: own org + victim's execution id → nothing.
    r = await client.get(
        f"/api/v1/orgs/{attacker_org}/usage/records",
        params={"execution_id": victim_exec},
        headers=attacker.headers,
    )
    assert r.json() == []
    r = await client.get(
        f"/api/v1/orgs/{attacker_org}/usage/summary",
        params={"execution_id": victim_exec},
        headers=attacker.headers,
    )
    assert r.json() == []


ROLES = ["viewer", "operator", "admin", "owner"]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    ("method", "path", "body", "min_role"),
    [
        ("GET", "/usage/summary", None, "operator"),
        ("GET", "/usage/records", None, "operator"),
        ("GET", "/budget", None, "operator"),
        ("PUT", "/budget", {"monthly_limit_usd": "10"}, "admin"),
    ],
)
async def test_usage_rbac(
    api: Api,
    client: AsyncClient,
    role: str,
    method: str,
    path: str,
    body: dict | None,  # type: ignore[type-arg]
    min_role: str,
) -> None:
    owner = await api.user()
    org = await api.org(owner)
    actor = owner if role == "owner" else await api.member(org, owner, role)
    r = await client.request(method, f"/api/v1/orgs/{org}{path}", json=body, headers=actor.headers)
    expected_ok = ROLES.index(role) >= ROLES.index(min_role)
    assert r.status_code == (200 if expected_ok else 403), r.text


def test_provider_api_key_is_never_rendered() -> None:
    key = "sk-ant-api03-SUPERSECRETVALUE"
    s = Settings(environment=Environment.TEST, anthropic_api_key=key)  # type: ignore[arg-type]
    assert key not in repr(s) and key not in str(s.model_dump())


def test_usage_ledger_stores_no_prompt_or_output_content() -> None:
    """Data minimisation: the billing ledger must not become a copy of customer data."""
    columns = {c.name for c in UsageRecord.__table__.columns}
    assert not columns & {"prompt", "input", "output", "content", "messages", "text", "response"}


async def test_prompt_content_not_persisted_in_usage_or_audit(
    api: Api, client: AsyncClient, engine: Engine, mock_llm: MockProvider
) -> None:
    s = await make_workflow(api, LLM_WF)
    eid = await start(api, s)
    await engine.run_until_idle()
    records = (await client.get(s.url("/usage/records"), headers=s.owner.headers)).json()
    audit = (await client.get(s.url("/audit-events"), headers=s.owner.headers)).json()
    assert records and "4111" not in str(records) and "4111" not in str(audit)
    assert eid  # the execution itself (owned data) does hold the step output, by design
