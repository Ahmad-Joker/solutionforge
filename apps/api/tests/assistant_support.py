"""Test support for the assistant: a demo-corpus knowledge base and a scripted provider."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from solutionforge.assistant.corpus import load_corpus, seed_tickets, sync_documents
from solutionforge.assistant.service import AssistantService
from solutionforge.db.tenancy import TenantContext
from solutionforge.llm.pricing import DEFAULT_PRICES, ModelPrice, PriceTable
from solutionforge.llm.types import (
    ConverseRequest,
    ConverseResponse,
    LLMError,
    ToolCall,
    Usage,
)
from solutionforge.retrieval.ingest import IngestionWorker
from solutionforge.security.rbac import Role
from tests.helpers import Api, Session

SCRIPTED = "scripted:m1"
# Synthetic test price: $1 / $2 per MTok input / output.
SCRIPTED_PRICE = ModelPrice.of("1", "2", "0.1", "1.25", "2")

Step = ConverseResponse | LLMError | Callable[[ConverseRequest], ConverseResponse]


@dataclass
class ScriptedProvider:
    """Plays back a fixed list of responses (or raises scripted errors). Each step may be a
    callable that builds the response from the request, e.g. to cite a retrieved ID."""

    steps: list[Step]
    name: str = "scripted"
    requests: list[ConverseRequest] = field(default_factory=list)

    async def converse(self, request: ConverseRequest) -> ConverseResponse:
        self.requests.append(request)
        if not self.steps:
            raise AssertionError("scripted provider ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, LLMError):
            raise step
        if callable(step):
            return step(request)
        return step


def tool_use(
    name: str, args: dict[str, Any], *, call_id: str | None = None, usage: Usage | None = None
) -> ConverseResponse:
    call = ToolCall(id=call_id or f"toolu_{uuid.uuid4().hex[:8]}", name=name, input=args)
    return ConverseResponse(
        text="",
        tool_calls=(call,),
        content=({"type": "tool_use", "id": call.id, "name": name, "input": args},),
        stop_reason="tool_use",
        usage=usage or Usage(100, 10),
    )


def final(text: str, *, stop: str = "end_turn", usage: Usage | None = None) -> ConverseResponse:
    return ConverseResponse(
        text=text,
        tool_calls=(),
        content=({"type": "text", "text": text},),
        stop_reason=stop,
        usage=usage or Usage(200, 30),
    )


def last_tool_results(request: ConverseRequest) -> list[dict[str, Any]]:
    content = request.messages[-1]["content"]
    assert isinstance(content, list)
    return [b for b in content if b["type"] == "tool_result"]


def cite_first_passage(request: ConverseRequest) -> ConverseResponse:
    payload = json.loads(last_tool_results(request)[0]["content"])
    sid = payload["passages"][0]["source_id"]
    return final(f"Here is the answer. [{sid}]")


@dataclass
class Demo:
    owner: Session
    org: str
    kb: str
    ctx: TenantContext
    svc: AssistantService


async def demo_org(
    api: Api,
    client: AsyncClient,
    app: FastAPI,
    ingestor: IngestionWorker,
    *,
    injection: bool = False,
) -> Demo:
    owner = await api.user()
    org = await api.org(owner)
    r = await client.post(
        f"/api/v1/orgs/{org}/knowledge-bases",
        json={"name": "Harbor & Pine help centre", "chunk_size": 800, "chunk_overlap": 120},
        headers=owner.headers,
    )
    assert r.status_code == 201, r.text
    kb = r.json()["id"]
    ctx = TenantContext(uuid.UUID(org), uuid.UUID(owner.user_id), Role.OWNER)
    await sync_documents(
        app.state.sessionmaker, ctx, uuid.UUID(kb), load_corpus(include_injection=injection)
    )
    await ingestor.run_until_idle()
    await seed_tickets(app.state.sessionmaker, ctx.organization_id)
    svc: AssistantService = app.state.assistant
    svc.prices = PriceTable({**DEFAULT_PRICES, SCRIPTED: SCRIPTED_PRICE})

    async def no_sleep(_: float) -> None:
        return None

    svc.sleep = no_sleep
    return Demo(owner, org, kb, ctx, svc)
