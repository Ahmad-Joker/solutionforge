"""Knowledge bases, documents and search (tenant-scoped, permission-checked)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solutionforge.core.clock import utcnow
from solutionforge.core.errors import Conflict, NotFound, ValidationFailed
from solutionforge.db.tenancy import TenantContext, scoped_select
from solutionforge.domain.audit import AuditEventType
from solutionforge.domain.knowledge import Document, DocumentStatus, KnowledgeBase
from solutionforge.retrieval.search import FILTER_KEY, Retriever, SearchHit, Strategy
from solutionforge.security.rbac import Permission
from solutionforge.services import audit_service
from solutionforge.services.audit_service import RequestMeta
from solutionforge.services.authz import ensure

MAX_DOCUMENT_CHARS = 1_000_000


async def create_kb(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    name: str,
    description: str,
    embedder: str,
    chunk_size: int,
    chunk_overlap: int,
    request: RequestMeta,
) -> KnowledgeBase:
    ensure(ctx, Permission.KNOWLEDGE_WRITE)
    if chunk_overlap >= chunk_size // 2:
        raise ValidationFailed("chunk_overlap must be less than half of chunk_size")
    kb = KnowledgeBase(
        organization_id=ctx.organization_id,
        name=name,
        description=description,
        embedder=embedder,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        created_by_user_id=ctx.user_id,
    )
    session.add(kb)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("A knowledge base with this name already exists") from exc
    audit_service.record(
        session,
        event_type=AuditEventType.KB_CREATED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="knowledge_base",
        resource_id=kb.id,
        metadata={"name": name},
    )
    await session.commit()
    return kb


async def list_kbs(session: AsyncSession, ctx: TenantContext) -> list[KnowledgeBase]:
    ensure(ctx, Permission.KNOWLEDGE_READ)
    return list(
        (
            await session.scalars(scoped_select(KnowledgeBase, ctx).order_by(KnowledgeBase.name))
        ).all()
    )


async def get_kb(session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID) -> KnowledgeBase:
    ensure(ctx, Permission.KNOWLEDGE_READ)
    kb = await session.scalar(scoped_select(KnowledgeBase, ctx, KnowledgeBase.id == kb_id))
    if kb is None:
        raise NotFound("Knowledge base not found")
    return kb


async def delete_kb(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID, *, request: RequestMeta
) -> None:
    ensure(ctx, Permission.KNOWLEDGE_WRITE)
    kb = await get_kb(session, ctx, kb_id)
    await session.delete(kb)  # documents and chunks cascade
    audit_service.record(
        session,
        event_type=AuditEventType.KB_DELETED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="knowledge_base",
        resource_id=kb_id,
        metadata={"name": kb.name},
    )
    await session.commit()


async def add_document(
    session: AsyncSession,
    ctx: TenantContext,
    kb_id: uuid.UUID,
    *,
    title: str,
    content: str,
    metadata: dict[str, str],
    source_uri: str | None,
    request: RequestMeta,
) -> tuple[Document, bool]:
    """Queue a document for ingestion. Identical content in the same KB returns the existing
    document (created=False) instead of indexing it twice."""
    ensure(ctx, Permission.KNOWLEDGE_WRITE)
    await get_kb(session, ctx, kb_id)
    if not content.strip():
        raise ValidationFailed("document content is empty")
    if len(content) > MAX_DOCUMENT_CHARS:
        raise ValidationFailed(f"document exceeds {MAX_DOCUMENT_CHARS} characters")
    bad = [k for k in metadata if not FILTER_KEY.match(k)]
    if bad:
        raise ValidationFailed(f"invalid metadata keys: {bad}")
    digest = hashlib.sha256(content.encode()).hexdigest()

    existing = await session.scalar(
        scoped_select(
            Document, ctx, Document.knowledge_base_id == kb_id, Document.content_hash == digest
        )
    )
    if existing is not None:
        return existing, False
    doc = Document(
        organization_id=ctx.organization_id,
        knowledge_base_id=kb_id,
        title=title,
        content=content,
        content_hash=digest,
        doc_metadata=metadata,
        source_uri=source_uri,
        status=DocumentStatus.PENDING,
        created_by_user_id=ctx.user_id,
    )
    session.add(doc)
    try:
        await session.flush()
    except IntegrityError:  # concurrent upload of the same content
        await session.rollback()
        existing = await session.scalar(
            scoped_select(
                Document, ctx, Document.knowledge_base_id == kb_id, Document.content_hash == digest
            )
        )
        if existing is None:
            raise
        return existing, False
    audit_service.record(
        session,
        event_type=AuditEventType.DOCUMENT_ADDED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="document",
        resource_id=doc.id,
        metadata={"knowledge_base_id": str(kb_id), "title": title, "chars": len(content)},
    )
    await session.commit()
    return doc, True


async def list_documents(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID, *, status: DocumentStatus | None
) -> list[Document]:
    await get_kb(session, ctx, kb_id)
    stmt = scoped_select(Document, ctx, Document.knowledge_base_id == kb_id)
    if status is not None:
        stmt = stmt.where(Document.status == status)
    return list((await session.scalars(stmt.order_by(Document.created_at))).all())


async def get_document(session: AsyncSession, ctx: TenantContext, doc_id: uuid.UUID) -> Document:
    ensure(ctx, Permission.KNOWLEDGE_READ)
    doc = await session.scalar(scoped_select(Document, ctx, Document.id == doc_id))
    if doc is None:
        raise NotFound("Document not found")
    return doc


async def delete_document(
    session: AsyncSession, ctx: TenantContext, doc_id: uuid.UUID, *, request: RequestMeta
) -> None:
    ensure(ctx, Permission.KNOWLEDGE_WRITE)
    doc = await get_document(session, ctx, doc_id)
    await session.delete(doc)
    audit_service.record(
        session,
        event_type=AuditEventType.DOCUMENT_DELETED,
        request=request,
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        resource_type="document",
        resource_id=doc_id,
        metadata={"title": doc.title},
    )
    await session.commit()


async def retry_document(session: AsyncSession, ctx: TenantContext, doc_id: uuid.UUID) -> Document:
    """Re-queue a dead-lettered document."""
    ensure(ctx, Permission.KNOWLEDGE_WRITE)
    doc = await get_document(session, ctx, doc_id)
    if doc.status != DocumentStatus.FAILED:
        raise Conflict(f"Document is {doc.status.value}; only failed documents can be retried")
    doc.status = DocumentStatus.PENDING
    doc.attempts = 0
    doc.next_attempt_at = utcnow()
    await session.commit()
    return doc


async def search(
    session: AsyncSession,
    ctx: TenantContext,
    retriever: Retriever,
    kb_id: uuid.UUID,
    *,
    query: str,
    top_k: int,
    strategy: Strategy,
    filters: dict[str, str],
    min_dense_score: float,
) -> list[SearchHit]:
    await get_kb(session, ctx, kb_id)  # KNOWLEDGE_READ + tenant check
    # The retriever uses its own session. End this read-only transaction first so the
    # request's connection goes back to the pool: holding it while waiting for a second one
    # deadlocks the whole pool under concurrency (found by load testing: 30 connections
    # "idle in transaction", every endpoint stalled for the 30 s pool timeout).
    await session.rollback()
    try:
        return await retriever.search(
            organization_id=ctx.organization_id,
            knowledge_base_id=kb_id,
            query=query,
            top_k=top_k,
            strategy=strategy,
            filters=filters,
            min_dense_score=min_dense_score,
        )
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from None


async def kb_id_by_name(
    session: AsyncSession, organization_id: uuid.UUID, name: str
) -> uuid.UUID | None:
    """For workflow steps (system context): resolve a KB name within one organization."""
    return await session.scalar(
        sa.select(KnowledgeBase.id).where(
            KnowledgeBase.organization_id == organization_id, KnowledgeBase.name == name
        )
    )


def hit_dict(h: SearchHit, *, include_text: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "chunk_id": str(h.chunk_id),
        "document_id": str(h.document_id),
        "title": h.document_title,
        "section": h.section,
        "score": h.score,
        "char_start": h.char_start,
        "char_end": h.char_end,
        "sources": h.sources,
    }
    if include_text:
        out["text"] = h.text
    return out
