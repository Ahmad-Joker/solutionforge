"""Business assistant: tool loop, limits, retries, authorization and draft approval.

Regression coverage for the agent-safety properties: authorization in code, approval
replay, changed drafts, malformed and unknown tool calls, loop/tool/deadline/budget limits,
transient provider errors, cancellation, missing evidence and tenant boundaries. The model
is a scripted provider (or the labelled fixture policy); one test drives the real Anthropic
SDK over a mock HTTP transport.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import anthropic
import httpx2
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import AsyncClient

from solutionforge.assistant.service import AssistantConfig
from solutionforge.db.tenancy import TenantContext
from solutionforge.domain import SimTicket, TicketDraft
from solutionforge.domain.assistant import AssistantEvent, RunStatus
from solutionforge.llm.providers.claude import AnthropicProvider
from solutionforge.llm.types import (
    ConverseRequest,
    InvalidRequest,
    ProviderUnavailable,
    RateLimited,
    Usage,
)
from solutionforge.retrieval.ingest import IngestionWorker
from solutionforge.security.rbac import Role
from tests.assistant_support import (
    SCRIPTED,
    Demo,
    ScriptedProvider,
    demo_org,
    final,
    last_tool_results,
    tool_use,
)
from tests.helpers import Api


@pytest.fixture
async def demo(api: Api, client: AsyncClient, app: FastAPI, ingestor: IngestionWorker) -> Demo:
    return await demo_org(api, client, app, ingestor)


def cfg(model: str = SCRIPTED, **kw: Any) -> AssistantConfig:
    return AssistantConfig(model=model, backoff_base_seconds=0, **kw)


async def run_scripted(
    demo: Demo, steps: list[Any], question: str = "q?", **kw: Any
) -> tuple[Any, ScriptedProvider]:
    provider = ScriptedProvider(steps)
    demo.svc.providers["scripted"] = provider
    run = await demo.svc.run(
        demo.ctx, question=question, knowledge_base_id=uuid.UUID(demo.kb), config=cfg(**kw)
    )
    return run, provider


async def events(app: FastAPI, run_id: uuid.UUID) -> list[AssistantEvent]:
    async with app.state.sessionmaker() as s:
        return list(
            (
                await s.scalars(
                    sa.select(AssistantEvent)
                    .where(AssistantEvent.run_id == run_id)
                    .order_by(AssistantEvent.seq)
                )
            ).all()
        )


# --------------------------------------------------------------------------- happy paths


async def test_fixture_policy_answers_with_a_verified_source(
    client: AsyncClient, demo: Demo
) -> None:
    r = await client.post(
        f"/api/v1/orgs/{demo.org}/assistant/runs",
        json={
            "question": "How many days do I have to request a refund?",
            "knowledge_base_id": demo.kb,
            "model": "fixture:policy-v1",
        },
        headers=demo.owner.headers,
    )
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "completed", run
    # Both the refund policy and the refund procedure state the 30-day window.
    assert run["citations"] and run["citations"][0]["doc_id"] in {"HP-POL-02", "HP-SOP-04"}
    assert "30 days" in run["answer"]
    assert run["provider"] == "fixture" and run["prompt_version"] == "assistant-v1"
    assert run["corpus_version"].startswith("20docs-")
    ev = await client.get(
        f"/api/v1/orgs/{demo.org}/assistant/runs/{run['id']}/events", headers=demo.owner.headers
    )
    tools = [e for e in ev.json() if e["kind"] == "tool"]
    assert tools[0]["name"] == "search_documents"
    assert tools[0]["detail"]["args"]["query"].startswith("How many days")


async def test_ticket_lookup_and_missing_ticket(demo: Demo) -> None:
    run, _ = await run_scripted(
        demo,
        [tool_use("get_ticket_status", {"ticket_id": "T-1A2B3C4D"}), final("It is open.")],
    )
    assert run.status == RunStatus.COMPLETED
    run2, p2 = await run_scripted(
        demo,
        [tool_use("get_ticket_status", {"ticket_id": "T-00000000"}), final("Not found.")],
    )
    result = last_tool_results(p2.requests[1])[0]
    assert result["is_error"] is True and "No ticket T-00000000" in result["content"]
    assert run2.status == RunStatus.COMPLETED  # a definite "not found" answer is a completion


async def test_full_loop_through_the_real_sdk(app: FastAPI, demo: Demo) -> None:
    """Wire-level check: tools sent, tool_use parsed, tool_result returned with the same id."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        bodies.append(body)
        usage = {"input_tokens": 500, "output_tokens": 20}
        if len(bodies) == 1:
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_A",
                    "name": "search_documents",
                    "input": {"query": "emergency call-out fee"},
                }
            ]
            stop = "tool_use"
        else:
            result = body["messages"][-1]["content"][0]
            sid = json.loads(result["content"])["passages"][0]["source_id"]
            content = [{"type": "text", "text": f"It is $189. [{sid}]"}]
            stop = "end_turn"
        return httpx2.Response(
            200,
            json={
                "id": f"msg_{len(bodies)}",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": content,
                "stop_reason": stop,
                "stop_sequence": None,
                "usage": usage,
            },
        )

    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test-not-a-real-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    demo.svc.providers["anthropic"] = AnthropicProvider(client=client)
    try:
        run = await demo.svc.run(
            demo.ctx,
            question="What is the out-of-hours call-out fee?",
            knowledge_base_id=uuid.UUID(demo.kb),
            config=cfg("anthropic:claude-haiku-5-5"),
        )
    finally:
        del demo.svc.providers["anthropic"]
    assert [t["name"] for t in bodies[0]["tools"]] == [
        "search_documents",
        "get_ticket_status",
        "create_ticket_draft",
    ]
    second = bodies[1]["messages"]
    assert second[1]["content"][0]["id"] == "toolu_A"
    assert second[2]["content"][0]["tool_use_id"] == "toolu_A"
    assert run.status == RunStatus.COMPLETED and run.citations
    # 2 calls x (500 in, 20 out) at Haiku 5.5 list prices ($0.10 / $0.50 per MTok).
    assert (run.input_tokens, run.output_tokens) == (1000, 40)
    assert run.cost_micro_usd == 100 + 20


# --------------------------------------------------------------------------- bad tool calls


async def test_unknown_tool_and_malformed_arguments_go_back_as_errors(
    app: FastAPI, demo: Demo
) -> None:
    run, p = await run_scripted(
        demo,
        [
            tool_use("delete_all_tickets", {}),
            tool_use("get_ticket_status", {"ticket_id": "ticket 1234"}),
            tool_use("search_documents", {"query": "x" * 400, "extra": 1}),
            final("Sorry, please check the ticket ID."),
        ],
    )
    errs = [last_tool_results(r)[0] for r in p.requests[1:]]
    assert all(e["is_error"] for e in errs)
    assert "Unknown tool 'delete_all_tickets'" in errs[0]["content"]
    assert "must look like T-1A2B3C4D" in errs[1]["content"]
    assert "query" in errs[2]["content"] and "extra" in errs[2]["content"]
    outcomes = [e.outcome for e in await events(app, run.id) if e.kind == "tool"]
    assert outcomes == ["unknown_tool", "invalid_arguments", "invalid_arguments"]
    assert run.status == RunStatus.INSUFFICIENT_EVIDENCE  # no source, no lookup, no draft


async def test_fabricated_citation_is_removed_and_recorded(app: FastAPI, demo: Demo) -> None:
    run, _ = await run_scripted(
        demo,
        [tool_use("search_documents", {"query": "warranty"}), final("Ten years. [HP-POL-99#0]")],
    )
    assert "[HP-POL-99#0]" not in run.answer
    assert run.status == RunStatus.INSUFFICIENT_EVIDENCE
    checks = [e for e in await events(app, run.id) if e.kind == "check"]
    assert checks[0].detail["invalid"] == ["HP-POL-99#0"]


# --------------------------------------------------------------------------- limits


async def test_turn_limit(demo: Demo) -> None:
    run, _ = await run_scripted(
        demo, [tool_use("search_documents", {"query": "fees"}) for _ in range(3)], max_turns=3
    )
    assert run.status == RunStatus.LIMIT_REACHED and "turn limit" in run.error


async def test_tool_call_limit(demo: Demo) -> None:
    run, p = await run_scripted(
        demo,
        [tool_use("search_documents", {"query": f"fees {i}"}) for i in range(3)] + [final("x")],
        max_tool_calls=2,
    )
    assert run.status == RunStatus.LIMIT_REACHED and run.tool_calls == 2
    assert len(p.requests) == 3


async def test_deadline_bounds_a_hanging_provider(demo: Demo) -> None:
    class Hanging:
        name = "scripted"

        async def converse(self, request: ConverseRequest) -> Any:
            await asyncio.sleep(30)

    demo.svc.providers["scripted"] = Hanging()  # type: ignore[assignment]
    run = await demo.svc.run(
        demo.ctx,
        question="q",
        knowledge_base_id=uuid.UUID(demo.kb),
        config=cfg(deadline_seconds=0.3, max_retries=5),
    )
    assert run.status == RunStatus.LIMIT_REACHED and "deadline" in run.error
    assert run.latency_ms < 5000


async def test_run_budget(demo: Demo) -> None:
    big = Usage(input_tokens=60_000, output_tokens=0)  # $0.06 at the synthetic $1/MTok
    run, _ = await run_scripted(
        demo,
        [tool_use("search_documents", {"query": "fees"}, usage=big), final("x")],
        run_budget_micro_usd=50_000,
    )
    assert run.status == RunStatus.LIMIT_REACHED and "budget" in run.error
    assert run.cost_micro_usd == 60_000


# --------------------------------------------------------------------------- provider errors


async def test_transient_errors_are_retried_and_their_usage_counted(
    app: FastAPI, demo: Demo
) -> None:
    run, _ = await run_scripted(
        demo,
        [
            RateLimited("slow down", retry_after=0, usage=Usage(10, 0)),
            ProviderUnavailable("overloaded"),
            tool_use("get_ticket_status", {"ticket_id": "T-1A2B3C4D"}),
            final("Open."),
        ],
    )
    assert run.status == RunStatus.COMPLETED and run.retries == 2
    assert run.input_tokens == 10 + 100 + 200
    provider_events = [e.outcome for e in await events(app, run.id) if e.kind == "provider"]
    assert provider_events[:2] == ["rate_limited", "provider_unavailable"]


async def test_retries_are_bounded(demo: Demo) -> None:
    run, p = await run_scripted(demo, [ProviderUnavailable("down")] * 3, max_retries=2)
    assert run.status == RunStatus.FAILED and "after 3 attempts" in run.error
    assert len(p.requests) == 3


async def test_non_transient_error_fails_fast(demo: Demo) -> None:
    run, p = await run_scripted(demo, [InvalidRequest("bad"), final("never")])
    assert run.status == RunStatus.FAILED and len(p.requests) == 1


async def test_cancellation_between_steps(demo: Demo) -> None:
    def cancel_then_tool(request: ConverseRequest) -> Any:
        return tool_use("search_documents", {"query": "fees"})

    provider = ScriptedProvider([cancel_then_tool, final("never")])
    demo.svc.providers["scripted"] = provider
    original = demo.svc._search

    async def search_and_cancel(st: Any, args: Any) -> Any:
        await demo.svc.cancel(demo.ctx, st.id)
        return await original(st, args)

    demo.svc._search = search_and_cancel  # type: ignore[method-assign]
    run = await demo.svc.run(
        demo.ctx, question="q", knowledge_base_id=uuid.UUID(demo.kb), config=cfg()
    )
    assert run.status == RunStatus.CANCELLED
    assert len(provider.requests) == 1


# --------------------------------------------------------------------------- authorization


async def test_viewer_cannot_run_or_approve(api: Api, client: AsyncClient, demo: Demo) -> None:
    viewer = await api.member(demo.org, demo.owner, "viewer")
    r = await client.post(
        f"/api/v1/orgs/{demo.org}/assistant/runs",
        json={"question": "hi", "knowledge_base_id": demo.kb, "model": "fixture:policy-v1"},
        headers=viewer.headers,
    )
    assert r.status_code == 403
    await run_scripted(
        demo,
        [
            tool_use(
                "create_ticket_draft", {"subject": "Leak", "description": "Kitchen tap leaks."}
            ),
            final("Drafted."),
        ],
    )
    draft = (
        await client.get(f"/api/v1/orgs/{demo.org}/assistant/drafts", headers=demo.owner.headers)
    ).json()[0]
    r = await client.post(
        f"/api/v1/orgs/{demo.org}/assistant/drafts/{draft['id']}/approve",
        json={"content_hash": draft["content_hash"]},
        headers=viewer.headers,
    )
    assert r.status_code == 403


async def test_tool_permissions_follow_the_user_not_the_model(demo: Demo) -> None:
    """A role without knowledge:read gets an is_error result even if the model asks."""
    # Operators can search; simulate a permission this role lacks by re-gating the tool.
    demo.ctx = TenantContext(demo.ctx.organization_id, demo.ctx.user_id, Role.OPERATOR)
    from solutionforge.assistant import tools as t
    from solutionforge.security.rbac import Permission

    spec, model, _ = t.TOOLS["search_documents"]
    t.TOOLS["search_documents"] = (spec, model, Permission.ORG_DELETE)  # owner-only
    try:
        _, p = await run_scripted(
            demo, [tool_use("search_documents", {"query": "fees"}), final("x")]
        )
    finally:
        t.TOOLS["search_documents"] = (spec, model, Permission.KNOWLEDGE_READ)
    result = last_tool_results(p.requests[1])[0]
    assert result["is_error"] and "Not permitted" in result["content"]


async def test_tenant_boundary_on_tickets_and_drafts(
    api: Api, client: AsyncClient, app: FastAPI, ingestor: IngestionWorker, demo: Demo
) -> None:
    other = await demo_org(api, client, app, ingestor)
    async with app.state.sessionmaker() as s, s.begin():
        s.add(
            SimTicket(
                organization_id=other.ctx.organization_id,
                ref="T-ABCDEF01",
                subject="private",
                body="x",
                priority="low",
                status="open",
            )
        )
    _, p = await run_scripted(
        demo, [tool_use("get_ticket_status", {"ticket_id": "T-ABCDEF01"}), final("x")]
    )
    assert "No ticket T-ABCDEF01" in last_tool_results(p.requests[1])[0]["content"]
    await run_scripted(
        demo,
        [
            tool_use(
                "create_ticket_draft", {"subject": "Leak", "description": "Kitchen tap leaks."}
            ),
            final("ok"),
        ],
    )
    draft = (
        await client.get(f"/api/v1/orgs/{demo.org}/assistant/drafts", headers=demo.owner.headers)
    ).json()[0]
    r = await client.post(
        f"/api/v1/orgs/{other.org}/assistant/drafts/{draft['id']}/approve",
        json={"content_hash": draft["content_hash"]},
        headers=other.owner.headers,
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- drafts & approval


async def _draft(client: AsyncClient, demo: Demo) -> dict[str, Any]:
    await run_scripted(
        demo,
        [
            tool_use(
                "create_ticket_draft",
                {"subject": "Dripping tap", "description": "The kitchen tap drips all night."},
            ),
            final("Draft ready."),
        ],
    )
    drafts = (
        await client.get(f"/api/v1/orgs/{demo.org}/assistant/drafts", headers=demo.owner.headers)
    ).json()
    return drafts[0]


async def test_draft_is_not_a_ticket_until_approved_and_replay_is_idempotent(
    app: FastAPI, client: AsyncClient, demo: Demo
) -> None:
    draft = await _draft(client, demo)
    assert draft["status"] == "pending" and draft["ticket_ref"] is None
    async with app.state.sessionmaker() as s:
        before = await s.scalar(
            sa.select(sa.func.count())
            .select_from(SimTicket)
            .where(SimTicket.organization_id == demo.ctx.organization_id)
        )
    url = f"/api/v1/orgs/{demo.org}/assistant/drafts/{draft['id']}/approve"
    body = {"content_hash": draft["content_hash"]}
    first = await client.post(url, json=body, headers=demo.owner.headers)
    second = await client.post(url, json=body, headers=demo.owner.headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["ticket_created"] is True and second.json()["ticket_created"] is False
    ref = first.json()["draft"]["ticket_ref"]
    assert second.json()["draft"]["ticket_ref"] == ref
    async with app.state.sessionmaker() as s:
        after = await s.scalar(
            sa.select(sa.func.count())
            .select_from(SimTicket)
            .where(SimTicket.organization_id == demo.ctx.organization_id)
        )
    assert after == before + 1


async def test_concurrent_approvals_create_one_ticket(
    app: FastAPI, client: AsyncClient, demo: Demo
) -> None:
    draft = await _draft(client, demo)
    url = f"/api/v1/orgs/{demo.org}/assistant/drafts/{draft['id']}/approve"
    rs = await asyncio.gather(
        *[
            client.post(
                url, json={"content_hash": draft["content_hash"]}, headers=demo.owner.headers
            )
            for _ in range(4)
        ]
    )
    assert {r.status_code for r in rs} == {200}
    assert len({r.json()["draft"]["ticket_ref"] for r in rs}) == 1
    async with app.state.sessionmaker() as s:
        n = await s.scalar(
            sa.select(sa.func.count())
            .select_from(SimTicket)
            .where(SimTicket.idempotency_key == f"ticket-draft:{draft['id']}")
        )
    assert n == 1


async def test_changed_draft_requires_a_fresh_review(client: AsyncClient, demo: Demo) -> None:
    draft = await _draft(client, demo)
    base = f"/api/v1/orgs/{demo.org}/assistant/drafts/{draft['id']}"
    r = await client.patch(
        base,
        json={"subject": "Dripping kitchen tap", "description": draft["description"]},
        headers=demo.owner.headers,
    )
    assert r.status_code == 200
    new_hash = r.json()["content_hash"]
    assert new_hash != draft["content_hash"]
    stale = await client.post(
        f"{base}/approve", json={"content_hash": draft["content_hash"]}, headers=demo.owner.headers
    )
    assert stale.status_code == 409
    ok = await client.post(
        f"{base}/approve", json={"content_hash": new_hash}, headers=demo.owner.headers
    )
    assert ok.status_code == 200 and ok.json()["draft"]["subject"] == "Dripping kitchen tap"
    edit_after = await client.patch(
        base, json={"subject": "x" * 5, "description": "y" * 20}, headers=demo.owner.headers
    )
    assert edit_after.status_code == 409


async def test_rejected_draft_cannot_be_approved(client: AsyncClient, demo: Demo) -> None:
    draft = await _draft(client, demo)
    base = f"/api/v1/orgs/{demo.org}/assistant/drafts/{draft['id']}"
    assert (await client.post(f"{base}/reject", headers=demo.owner.headers)).status_code == 200
    r = await client.post(
        f"{base}/approve", json={"content_hash": draft["content_hash"]}, headers=demo.owner.headers
    )
    assert r.status_code == 409


async def test_duplicate_draft_calls_are_deduplicated(app: FastAPI, demo: Demo) -> None:
    args = {"subject": "Dripping tap", "description": "The kitchen tap drips all night."}
    run, p = await run_scripted(
        demo,
        [
            tool_use("create_ticket_draft", args),
            tool_use("create_ticket_draft", args),
            tool_use("create_ticket_draft", {**args, "subject": "Another one"}),
            final("Done."),
        ],
    )
    async with app.state.sessionmaker() as s:
        drafts = (await s.scalars(sa.select(TicketDraft).where(TicketDraft.run_id == run.id))).all()
    assert len(drafts) == 1
    third = last_tool_results(p.requests[3])[0]
    assert third["is_error"] and "Only 1 ticket draft" in third["content"]


# --------------------------------------------------------------------------- evidence


async def test_unsupported_question_abstains_under_fixture(client: AsyncClient, demo: Demo) -> None:
    r = await client.post(
        f"/api/v1/orgs/{demo.org}/assistant/runs",
        json={
            "question": "Do you clean swimming pools?",
            "knowledge_base_id": demo.kb,
            "model": "fixture:policy-v1",
        },
        headers=demo.owner.headers,
    )
    run = r.json()
    assert run["status"] == "insufficient_evidence" and run["citations"] == []
    assert "couldn't find" in run["answer"]


async def test_unconfigured_provider_is_rejected(client: AsyncClient, demo: Demo) -> None:
    r = await client.post(
        f"/api/v1/orgs/{demo.org}/assistant/runs",
        json={"question": "hi", "knowledge_base_id": demo.kb, "model": "nope:model"},
        headers=demo.owner.headers,
    )
    assert r.status_code == 422 and "fixture:policy-v1" in r.text


async def test_info_lists_fixture_when_no_key(client: AsyncClient, demo: Demo) -> None:
    r = await client.get(f"/api/v1/orgs/{demo.org}/assistant", headers=demo.owner.headers)
    assert r.status_code == 200
    assert r.json()["models"] == ["fixture:policy-v1"]


async def test_demo_setup_is_idempotent_and_needs_write_access(
    api: Api, client: AsyncClient, ingestor: IngestionWorker
) -> None:
    owner = await api.user()
    org = await api.org(owner)
    url = f"/api/v1/orgs/{org}/assistant/demo-setup"
    first = await client.post(url, headers=owner.headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True and body["documents"]["added"] == 20
    assert body["tickets_inserted"] == 4 and "T-1A2B3C4D" in body["demo_ticket_ids"]
    await ingestor.run_until_idle()
    again = (await client.post(url, headers=owner.headers)).json()
    assert again["created"] is False and again["knowledge_base_id"] == body["knowledge_base_id"]
    assert again["documents"] == {"added": 0, "unchanged": 20, "replaced": 0}
    assert again["tickets_inserted"] == 0
    viewer = await api.member(org, owner, "viewer")
    assert (await client.post(url, headers=viewer.headers)).status_code == 403


async def test_changed_document_replaces_its_previous_version(app: FastAPI, demo: Demo) -> None:
    from dataclasses import replace

    from solutionforge.assistant.corpus import load_corpus, sync_documents
    from solutionforge.domain import Document

    docs = load_corpus()
    changed = [
        replace(d, body=d.body + "\nUpdated.\n") if d.doc_id == "HP-POL-02" else d for d in docs
    ]
    stats = await sync_documents(app.state.sessionmaker, demo.ctx, uuid.UUID(demo.kb), changed)
    assert (stats.added, stats.unchanged, stats.replaced) == (0, 19, 1)
    async with app.state.sessionmaker() as s:
        copies = (
            await s.scalars(
                sa.select(Document).where(
                    Document.knowledge_base_id == uuid.UUID(demo.kb),
                    Document.doc_metadata["doc_id"].as_string() == "HP-POL-02",
                )
            )
        ).all()
    assert len(copies) == 1 and copies[0].content.endswith("Updated.\n")
