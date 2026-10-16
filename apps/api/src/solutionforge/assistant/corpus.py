"""Load the Harbor & Pine demo corpus and sync it into a knowledge base, safely re-runnable.

Sync rules: a document is identified by its front-matter ``doc_id``. Unchanged content is
skipped; changed content replaces every older document with that ``doc_id`` (deleted, so
its chunks go too), so a knowledge base never holds two live versions of one document.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.db.tenancy import TenantContext, scoped_select
from solutionforge.domain.knowledge import Document
from solutionforge.domain.simulated import SimTicket
from solutionforge.services import kb_service
from solutionforge.services.audit_service import RequestMeta


def _find_corpus() -> Path:
    """The repo's ``corpus/harbor-pine`` (searched upwards), or ``SF_CORPUS_DIR``."""
    import os

    if env := os.environ.get("SF_CORPUS_DIR"):
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "corpus" / "harbor-pine"
        if candidate.is_dir():
            return candidate
    return Path("corpus") / "harbor-pine"


CORPUS_DIR = _find_corpus()
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
REQUIRED = ("doc_id", "title", "type", "version", "collection", "provenance", "demo")


@dataclass(frozen=True, slots=True)
class CorpusDoc:
    doc_id: str
    title: str
    body: str
    metadata: dict[str, str]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()


def parse_doc(text: str, origin: str = "") -> CorpusDoc:
    m = _FRONT.match(text)
    if not m:
        raise ValueError(f"{origin}: missing front matter")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"')
    missing = [k for k in REQUIRED if k not in meta]
    if missing:
        raise ValueError(f"{origin}: front matter lacks {missing}")
    if meta["demo"] != "true":
        raise ValueError(f"{origin}: corpus documents must be labelled demo: true")
    return CorpusDoc(meta["doc_id"], meta["title"], text[m.end() :].strip() + "\n", meta)


def load_corpus(root: Path = CORPUS_DIR, *, include_injection: bool = False) -> list[CorpusDoc]:
    folders = ["docs", "injection"] if include_injection else ["docs"]
    docs = [
        parse_doc(p.read_text(encoding="utf-8"), p.name)
        for folder in folders
        for p in sorted((root / folder).glob("*.md"))
    ]
    ids = [d.doc_id for d in docs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate doc_id in corpus")
    return docs


def load_tickets(root: Path = CORPUS_DIR) -> list[dict[str, str]]:
    tickets: list[dict[str, str]] = json.loads((root / "tickets.json").read_text("utf-8"))[
        "tickets"
    ]
    return tickets


@dataclass
class SyncStats:
    added: int = 0
    unchanged: int = 0
    replaced: int = 0


async def sync_documents(
    sessionmaker: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    kb_id: uuid.UUID,
    docs: list[CorpusDoc],
) -> SyncStats:
    stats = SyncStats()
    meta = RequestMeta()
    for doc in docs:
        async with sessionmaker() as s:
            existing = (
                await s.scalars(
                    scoped_select(Document, ctx, Document.knowledge_base_id == kb_id).where(
                        Document.doc_metadata["doc_id"].as_string() == doc.doc_id
                    )
                )
            ).all()
            if any(e.content_hash == doc.content_hash for e in existing) and len(existing) == 1:
                stats.unchanged += 1
                continue
            stale = [e.id for e in existing]
        for doc_uuid in stale:
            async with sessionmaker() as s:
                await kb_service.delete_document(s, ctx, doc_uuid, request=meta)
        async with sessionmaker() as s:
            await kb_service.add_document(
                s,
                ctx,
                kb_id,
                title=doc.title,
                content=doc.body,
                metadata=doc.metadata,
                source_uri=f"corpus/harbor-pine/{doc.metadata['collection']}/{doc.doc_id}.md",
                request=meta,
            )
            await s.commit()
        if stale:
            stats.replaced += 1
        else:
            stats.added += 1
    return stats


async def seed_tickets(
    sessionmaker: async_sessionmaker[AsyncSession], organization_id: uuid.UUID
) -> int:
    """Insert the demo tickets (fixed refs) if missing. Returns how many were inserted."""
    inserted = 0
    async with sessionmaker() as s, s.begin():
        have = set(
            (
                await s.scalars(
                    sa.select(SimTicket.ref).where(SimTicket.organization_id == organization_id)
                )
            ).all()
        )
        for t in load_tickets():
            if t["ref"] in have:
                continue
            s.add(
                SimTicket(
                    organization_id=organization_id,
                    ref=t["ref"],
                    subject=t["subject"],
                    body=f"Demo ticket: {t['subject']}",
                    priority=t["priority"],
                    status=t["status"],
                )
            )
            inserted += 1
    return inserted


DEMO_KB_NAME = "Harbor & Pine help centre (demo)"


@dataclass
class DemoSetup:
    knowledge_base_id: uuid.UUID
    embedder: str
    created: bool
    documents: SyncStats
    tickets_inserted: int


async def setup_demo(
    sessionmaker: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    *,
    embedder: str,
    request: RequestMeta,
) -> DemoSetup:
    """Create (or reuse) the demo knowledge base, sync the 20 core documents and seed the
    demo tickets. Idempotent. The injection documents are never loaded here. Ingestion
    itself runs in the background worker."""
    from solutionforge.domain.knowledge import KnowledgeBase

    async with sessionmaker() as s:
        kb = await s.scalar(scoped_select(KnowledgeBase, ctx, KnowledgeBase.name == DEMO_KB_NAME))
        created = kb is None
        if kb is None:
            kb = await kb_service.create_kb(
                s,
                ctx,
                name=DEMO_KB_NAME,
                description="Fictional demo content for the assistant (Harbor & Pine).",
                embedder=embedder,
                chunk_size=800,
                chunk_overlap=120,
                request=request,
            )
        kb_id, kb_embedder = kb.id, kb.embedder
    stats = await sync_documents(sessionmaker, ctx, kb_id, load_corpus())
    inserted = await seed_tickets(sessionmaker, ctx.organization_id)
    return DemoSetup(kb_id, kb_embedder, created, stats, inserted)
