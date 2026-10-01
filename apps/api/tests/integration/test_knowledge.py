"""Knowledge bases end-to-end: ingestion jobs, search strategies, benchmark, grounded answers."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.domain import Chunk, Document
from solutionforge.llm.providers.mock import MockProvider, MockReply
from solutionforge.retrieval.embeddings import HashingEmbedder
from solutionforge.retrieval.ingest import IngestionWorker
from solutionforge.retrieval.metrics import summarize
from solutionforge.workflows.engine import Engine
from tests.helpers import Api, Session
from tests.workflow_support import get_exec, make_workflow, start, step

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "support_kb.json").read_text())


class FailingEmbedder:
    name = "failing"
    dim = 1024

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding service unavailable")


async def kb_with_corpus(
    api: Api,
    client: AsyncClient,
    ingestor: IngestionWorker,
    owner: Session | None = None,
    name: str = "support",
    org: str | None = None,
) -> tuple[Session, str, str, dict[str, str]]:
    owner = owner or await api.user()
    org = org or await api.org(owner)
    r = await client.post(
        f"/api/v1/orgs/{org}/knowledge-bases",
        json={"name": name, "chunk_size": 400, "chunk_overlap": 60},
        headers=owner.headers,
    )
    assert r.status_code == 201, r.text
    kb = r.json()["id"]
    doc_ids: dict[str, str] = {}
    for d in FIXTURE["documents"]:
        r = await client.post(
            f"/api/v1/orgs/{org}/knowledge-bases/{kb}/documents",
            json={
                "title": d["title"],
                "content": d["content"],
                "metadata": {**d["metadata"], "slug": d["id"]},
            },
            headers=owner.headers,
        )
        assert r.status_code == 202, r.text
        doc_ids[r.json()["id"]] = d["id"]
    assert await ingestor.run_until_idle() == len(FIXTURE["documents"])
    return owner, org, kb, doc_ids


async def search(
    client: AsyncClient, owner: Session, org: str, kb: str, **body: Any
) -> list[dict[str, Any]]:
    r = await client.post(
        f"/api/v1/orgs/{org}/knowledge-bases/{kb}/search", json=body, headers=owner.headers
    )
    assert r.status_code == 200, r.text
    return r.json()["results"]  # type: ignore[no-any-return]


# ------------------------------------------------------------------ ingestion


async def test_upload_ingest_and_idempotent_reupload(
    api: Api, client: AsyncClient, ingestor: IngestionWorker, db: AsyncSession
) -> None:
    owner, org, kb, doc_ids = await kb_with_corpus(api, client, ingestor)
    docs = (
        await client.get(
            f"/api/v1/orgs/{org}/knowledge-bases/{kb}/documents", headers=owner.headers
        )
    ).json()
    assert {d["status"] for d in docs} == {"ready"} and all(d["chunk_count"] >= 1 for d in docs)

    first = FIXTURE["documents"][0]
    again = await client.post(
        f"/api/v1/orgs/{org}/knowledge-bases/{kb}/documents",
        json={"title": "dup", "content": first["content"]},
        headers=owner.headers,
    )
    assert again.status_code == 200 and doc_ids[again.json()["id"]] == first["id"]
    total_chunks = await db.scalar(sa.select(sa.func.count()).select_from(Chunk))
    assert total_chunks == sum(d["chunk_count"] for d in docs)


async def test_failed_ingestion_is_dead_lettered_and_retryable(
    api: Api, client: AsyncClient, app: FastAPI, db: AsyncSession
) -> None:
    owner = await api.user()
    org = await api.org(owner)
    kb = (
        await client.post(
            f"/api/v1/orgs/{org}/knowledge-bases", json={"name": "kb"}, headers=owner.headers
        )
    ).json()["id"]
    doc = (
        await client.post(
            f"/api/v1/orgs/{org}/knowledge-bases/{kb}/documents",
            json={"title": "t", "content": "some content about refunds"},
            headers=owner.headers,
        )
    ).json()
    broken = IngestionWorker(
        app.state.sessionmaker,
        FailingEmbedder(),
        worker_id="bad",
        max_attempts=2,
        backoff_base_seconds=60,
    )
    await broken.run_until_idle()  # attempt 1 -> pending, retry scheduled 60s out
    mid = (
        await client.get(f"/api/v1/orgs/{org}/documents/{doc['id']}", headers=owner.headers)
    ).json()
    assert (
        mid["status"] == "pending" and mid["attempts"] == 1 and "unavailable" in mid["last_error"]
    )
    assert await broken.claim() is None  # backoff respected: not due yet
    await db.execute(sa.update(Document).values(next_attempt_at=utcnow()))  # fast-forward
    await db.commit()
    await broken.run_until_idle()  # attempt 2 -> dead letter
    dead = (
        await client.get(f"/api/v1/orgs/{org}/documents/{doc['id']}", headers=owner.headers)
    ).json()
    assert dead["status"] == "failed" and dead["attempts"] == 2

    r = await client.post(f"/api/v1/orgs/{org}/documents/{doc['id']}/retry", headers=owner.headers)
    assert r.json()["status"] == "pending"
    healthy = IngestionWorker(app.state.sessionmaker, HashingEmbedder(), worker_id="good")
    await healthy.run_until_idle()
    done = (
        await client.get(f"/api/v1/orgs/{org}/documents/{doc['id']}", headers=owner.headers)
    ).json()
    assert done["status"] == "ready" and done["last_error"] is None
    assert (
        await client.post(f"/api/v1/orgs/{org}/documents/{doc['id']}/retry", headers=owner.headers)
    ).status_code == 409


async def test_crashed_ingestion_is_recovered_once(
    api: Api, client: AsyncClient, app: FastAPI, db: AsyncSession
) -> None:
    owner = await api.user()
    org = await api.org(owner)
    kb = (
        await client.post(
            f"/api/v1/orgs/{org}/knowledge-bases", json={"name": "kb"}, headers=owner.headers
        )
    ).json()["id"]
    await client.post(
        f"/api/v1/orgs/{org}/knowledge-bases/{kb}/documents",
        json={"title": "t", "content": "Refunds take 5 to 10 days.\n\nShipping is fast."},
        headers=owner.headers,
    )
    zombie = IngestionWorker(app.state.sessionmaker, HashingEmbedder(), worker_id="zombie")
    doc_id = await zombie.claim()  # claims, then "crashes" before processing
    assert doc_id is not None
    await db.execute(sa.update(Document).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
    await db.commit()
    rescuer = IngestionWorker(app.state.sessionmaker, HashingEmbedder(), worker_id="rescuer")
    assert await rescuer.run_until_idle() == 1
    # The zombie wakes up and tries to finish: fenced out, nothing duplicated.
    assert (await zombie.process(doc_id)).value == "ready"  # sees the rescuer's result
    chunks = await db.scalar(sa.select(sa.func.count()).select_from(Chunk))
    doc = (await db.scalars(sa.select(Document))).one()
    assert doc.status.value == "ready" and doc.chunk_count == chunks and doc.attempts == 2


async def test_document_validation(api: Api, client: AsyncClient) -> None:
    owner = await api.user()
    org = await api.org(owner)
    kb = (
        await client.post(
            f"/api/v1/orgs/{org}/knowledge-bases", json={"name": "kb"}, headers=owner.headers
        )
    ).json()["id"]
    url = f"/api/v1/orgs/{org}/knowledge-bases/{kb}/documents"
    for bad in (
        {"title": "t", "content": "   "},
        {"title": "t", "content": "x", "metadata": {"Bad Key": "v"}},
        {"title": "t", "content": "x", "extra": 1},
    ):
        assert (await client.post(url, json=bad, headers=owner.headers)).status_code == 422, bad
    r = await client.post(
        f"/api/v1/orgs/{org}/knowledge-bases",
        json={"name": "kb2", "chunk_size": 300, "chunk_overlap": 200},
        headers=owner.headers,
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ search


async def test_search_strategies_and_filters(
    api: Api, client: AsyncClient, ingestor: IngestionWorker
) -> None:
    owner, org, kb, doc_ids = await kb_with_corpus(api, client, ingestor)
    for strategy in ("dense", "keyword", "hybrid"):
        hits = await search(
            client, owner, org, kb, query="reset link expired password", strategy=strategy
        )
        assert hits and doc_ids[hits[0]["document_id"]] == "password", strategy
        assert hits[0]["text"] and hits[0]["char_end"] > hits[0]["char_start"]
    hybrid = await search(client, owner, org, kb, query="refund gift card", strategy="hybrid")
    assert set(hybrid[0]["sources"]) <= {"dense", "keyword"} and hybrid[0]["sources"]

    billing = await search(
        client, owner, org, kb, query="refund", filters={"category": "billing"}, top_k=20
    )
    assert billing and all(
        doc_ids[h["document_id"]] in {"refunds", "payment-methods", "invoices"} for h in billing
    )
    assert await search(client, owner, org, kb, query="refund", filters={"category": "nope"}) == []
    r = await client.post(
        f"/api/v1/orgs/{org}/knowledge-bases/{kb}/search",
        json={"query": "x", "filters": {"bad key!": "v"}},
        headers=owner.headers,
    )
    assert r.status_code == 422


async def test_retrieval_benchmark(
    api: Api, client: AsyncClient, ingestor: IngestionWorker, capsys: pytest.CaptureFixture[str]
) -> None:
    """Measured, not asserted-to-look-good: prints the table recorded in docs/RETRIEVAL.md
    and guards against regressions below the measured baseline."""
    owner, org, kb, doc_ids = await kb_with_corpus(api, client, ingestor)
    reports = {}
    for strategy in ("dense", "keyword", "hybrid"):
        results = []
        for q in FIXTURE["queries"]:
            hits = await search(client, owner, org, kb, query=q["q"], strategy=strategy, top_k=10)
            results.append(([doc_ids[h["document_id"]] for h in hits], set(q["relevant"])))
        reports[strategy] = summarize(strategy, results)
    with capsys.disabled():
        print("\n| strategy | queries | recall@1 | recall@3 | recall@5 | MRR |")
        for r in reports.values():
            print(r.row())
    for r in reports.values():
        assert r.recall_at_5 >= 0.70 and r.mrr >= 0.60, r


async def test_document_and_kb_deletion_cascade(
    api: Api, client: AsyncClient, ingestor: IngestionWorker, db: AsyncSession
) -> None:
    owner, org, kb, doc_ids = await kb_with_corpus(api, client, ingestor)
    some_doc = next(iter(doc_ids))
    assert (
        await client.delete(f"/api/v1/orgs/{org}/documents/{some_doc}", headers=owner.headers)
    ).status_code == 204
    hits = await search(client, owner, org, kb, query=FIXTURE["documents"][0]["title"], top_k=50)
    assert all(h["document_id"] != some_doc for h in hits)
    assert (
        await client.delete(f"/api/v1/orgs/{org}/knowledge-bases/{kb}", headers=owner.headers)
    ).status_code == 204
    assert await db.scalar(sa.select(sa.func.count()).select_from(Chunk)) == 0


# ------------------------------------------------------------------ RAG workflow


def rag_workflow(kb_name: str = "support") -> dict[str, Any]:
    return {
        "start": "find",
        "inputs": {"question": {"type": "string"}},
        "steps": [
            step(
                "find",
                "retrieve",
                {"knowledge_base": kb_name, "query": "$.input.question", "top_k": 3},
                next="answer",
            ),
            step(
                "answer",
                "grounded_answer",
                {"model": "mock:mock-1", "question": "$.input.question", "sources_from": "find"},
            ),
        ],
        "output": {"answer": "$.steps.answer.answer", "citations": "$.steps.answer.citations"},
    }


async def _rag_setup(
    api: Api, client: AsyncClient, ingestor: IngestionWorker
) -> tuple[Any, dict[str, str]]:
    s = await make_workflow(api, rag_workflow())
    _, _, _, doc_ids = await kb_with_corpus(api, client, ingestor, owner=s.owner, org=s.org)
    return s, doc_ids


async def test_grounded_answer_cites_real_retrieved_chunks(
    api: Api, client: AsyncClient, ingestor: IngestionWorker, engine: Engine, mock_llm: MockProvider
) -> None:
    s, _ = await _rag_setup(api, client, ingestor)
    mock_llm.script(
        MockReply.json(
            {
                "answer": "Refunds appear within 5 to 10 business days [S1].",
                "citations": ["S1"],
                "insufficient_context": False,
            }
        )
    )
    eid = await start(api, s, {"question": "how long do refunds take to appear"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded", ex["error"]
    [cite] = ex["output"]["citations"]
    retrieved = ex["steps"][0]["output"]["chunks"]
    assert cite["chunk_id"] == retrieved[0]["chunk_id"]  # S1 maps to the first retrieved chunk
    assert cite["quote"] and cite["title"] == retrieved[0]["title"]
    prompt = mock_llm.calls[0].messages[0]["content"]
    assert prompt.startswith("<sources>") and "[S1]" in prompt and "[S4]" not in prompt  # top_k=3


async def test_invented_or_unbacked_citations_are_rejected(
    api: Api, client: AsyncClient, ingestor: IngestionWorker, engine: Engine, mock_llm: MockProvider
) -> None:
    s, _ = await _rag_setup(api, client, ingestor)
    mock_llm.script(
        MockReply.json(
            {"answer": "Per the policy [S9].", "citations": ["S9"], "insufficient_context": False}
        ),
        MockReply.json(
            {"answer": "Per the policy [S2].", "citations": ["S1"], "insufficient_context": False}
        ),
        MockReply.json(
            {"answer": "Refunds take a while.", "citations": [], "insufficient_context": False}
        ),
        MockReply.json(
            {"answer": "Still unbacked [S5].", "citations": ["S1"], "insufficient_context": False}
        ),
    )
    eid = await start(api, s, {"question": "refund timing"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed"
    assert ex["error"]["code"] in {"citation_verification_failed", "llm_failed"}


async def test_citation_problems_get_feedback_and_can_be_fixed(
    api: Api, client: AsyncClient, ingestor: IngestionWorker, engine: Engine, mock_llm: MockProvider
) -> None:
    s, _ = await _rag_setup(api, client, ingestor)
    mock_llm.script(
        MockReply.json(
            {
                "answer": "Refunds take 5-10 days [S2].",
                "citations": ["S1"],
                "insufficient_context": False,
            }
        ),
        MockReply.json(
            {
                "answer": "Refunds take 5-10 days [S1].",
                "citations": ["S1"],
                "insufficient_context": False,
            }
        ),
    )
    eid = await start(api, s, {"question": "refund timing"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded"
    assert "Citation check failed" in mock_llm.calls[1].messages[-1]["content"]


async def test_no_sources_means_no_model_call(
    api: Api, client: AsyncClient, ingestor: IngestionWorker, engine: Engine, mock_llm: MockProvider
) -> None:
    s, _ = await _rag_setup(api, client, ingestor)
    eid = await start(api, s, {"question": "zzqx unrelated gibberish"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "succeeded"
    assert ex["output"]["citations"] == [] and mock_llm.calls == []
    assert ex["steps"][1]["output"]["insufficient_context"] is True


async def test_rag_step_config_validation(api: Api, client: AsyncClient) -> None:
    s = await make_workflow(api, {"start": "a", "steps": [step("a", "transform")]})
    bad = rag_workflow()
    bad["steps"][1]["config"]["sources_from"] = "ghost"
    r = await client.post(
        s.url(f"/workflows/{s.workflow}/versions"),
        json={"definition": bad},
        headers=s.owner.headers,
    )
    assert r.status_code == 422 and "unknown step" in str(r.json()["error"]["details"])


async def test_missing_knowledge_base_fails_cleanly(
    api: Api, client: AsyncClient, engine: Engine, mock_llm: MockProvider
) -> None:
    s = await make_workflow(api, rag_workflow("does-not-exist"))
    eid = await start(api, s, {"question": "q"})
    await engine.run_until_idle()
    ex = await get_exec(api, s, eid)
    assert ex["status"] == "failed" and ex["error"]["code"] == "knowledge_base_not_found"


_ = uuid  # keep import for readability of id handling above
