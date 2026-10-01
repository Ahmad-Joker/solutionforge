"""Durable document ingestion (chunk → embed → index) as leased background jobs.

Same reliability pattern as the workflow engine: ``SKIP LOCKED`` claiming with a lease,
attempts counted at claim time (a document that crashes its worker still exhausts its
attempts), fenced writes, exponential backoff between attempts, and a dead-letter state
(``failed``) that an operator can re-queue via the API.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.clock import utcnow
from solutionforge.core.logging import get_logger
from solutionforge.domain.knowledge import Chunk, Document, DocumentStatus, KnowledgeBase
from solutionforge.retrieval.chunking import chunk_text
from solutionforge.retrieval.embeddings import Embedder
from solutionforge.workflows.engine import default_worker_id

log = get_logger(__name__)
EMBED_BATCH = 64


class _LeaseLost(Exception):
    pass


class IngestionWorker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        worker_id: str | None = None,
        lease_seconds: float = 300,
        max_attempts: int = 3,
        backoff_base_seconds: float = 30,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self.worker_id = worker_id or default_worker_id()
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds

    async def claim(self) -> uuid.UUID | None:
        now = utcnow()
        runnable = sa.or_(
            sa.and_(Document.status == DocumentStatus.PENDING, Document.next_attempt_at <= now),
            sa.and_(Document.status == DocumentStatus.INGESTING, Document.lease_expires_at < now),
        )
        async with self.sessionmaker() as s, s.begin():
            candidate = await s.scalar(
                sa.select(Document.id)
                .where(runnable)
                .order_by(Document.next_attempt_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if candidate is None:
                return None
            res = await s.execute(
                sa.update(Document)
                .where(Document.id == candidate, runnable)
                .values(
                    status=DocumentStatus.INGESTING,
                    lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    attempts=Document.attempts + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            return candidate if res.rowcount == 1 else None  # type: ignore[attr-defined]

    async def process(self, document_id: uuid.UUID) -> DocumentStatus:
        async with self.sessionmaker() as s:
            row = (
                await s.execute(
                    sa.select(Document, KnowledgeBase)
                    .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
                    .where(Document.id == document_id)
                )
            ).first()
        if row is None:
            return DocumentStatus.FAILED
        doc, kb = row
        if doc.lease_owner != self.worker_id or doc.status != DocumentStatus.INGESTING:
            return doc.status
        try:
            pieces = chunk_text(doc.content, size=kb.chunk_size, overlap=kb.chunk_overlap)
            vectors: list[list[float]] = []
            for i in range(0, len(pieces), EMBED_BATCH):
                vectors += await self.embedder.embed([p.text for p in pieces[i : i + EMBED_BATCH]])
        except Exception as exc:  # parse/embed failure: retry or dead-letter
            return await self._fail(doc, exc)

        now = utcnow()
        try:
            await self._commit(doc, pieces, vectors, now)
        except _LeaseLost:
            log.warning("ingestion_lease_lost", document_id=str(doc.id))
            return DocumentStatus.INGESTING
        log.info("document_ingested", document_id=str(doc.id), chunks=len(pieces))
        return DocumentStatus.READY

    async def _commit(
        self, doc: Document, pieces: list[Any], vectors: list[list[float]], now: Any
    ) -> None:
        async with self.sessionmaker() as s, s.begin():
            fenced = await s.execute(
                sa.update(Document)
                .where(
                    Document.id == doc.id,
                    Document.lease_owner == self.worker_id,
                    Document.status == DocumentStatus.INGESTING,
                )
                .values(
                    status=DocumentStatus.READY,
                    chunk_count=len(pieces),
                    ingested_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if fenced.rowcount != 1:  # type: ignore[attr-defined]
                raise _LeaseLost  # rolls the whole transaction back
            # Re-ingestion replaces chunks atomically with the status flip.
            await s.execute(sa.delete(Chunk).where(Chunk.document_id == doc.id))
            s.add_all(
                Chunk(
                    organization_id=doc.organization_id,
                    knowledge_base_id=doc.knowledge_base_id,
                    document_id=doc.id,
                    ordinal=p.ordinal,
                    text=p.text,
                    char_start=p.char_start,
                    char_end=p.char_end,
                    section=p.section,
                    chunk_metadata=dict(doc.doc_metadata),
                    embedding=v,
                )
                for p, v in zip(pieces, vectors, strict=True)
            )

    async def _fail(self, doc: Document, exc: Exception) -> DocumentStatus:
        now = utcnow()
        dead = doc.attempts >= self.max_attempts
        status = DocumentStatus.FAILED if dead else DocumentStatus.PENDING
        message = f"{type(exc).__name__}: {str(exc)[:500]}"
        log.warning(
            "document_ingestion_failed",
            document_id=str(doc.id),
            attempt=doc.attempts,
            dead_lettered=dead,
            error_type=type(exc).__name__,
        )
        async with self.sessionmaker() as s, s.begin():
            await s.execute(
                sa.update(Document)
                .where(Document.id == doc.id, Document.lease_owner == self.worker_id)
                .values(
                    status=status,
                    last_error=message,
                    lease_owner=None,
                    lease_expires_at=None,
                    next_attempt_at=now
                    + timedelta(seconds=self.backoff_base_seconds * 2 ** (doc.attempts - 1)),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
        return status

    async def run_until_idle(self, max_documents: int = 1000) -> int:
        n = 0
        while n < max_documents and (doc_id := await self.claim()) is not None:
            await self.process(doc_id)
            n += 1
        return n

    async def run(self, stop: asyncio.Event, poll_interval: float = 1.0) -> None:
        while not stop.is_set():
            try:
                did = await self.run_until_idle(max_documents=10)
            except Exception:
                log.exception("ingestion_loop_error")
                did = 0
            if not did:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
