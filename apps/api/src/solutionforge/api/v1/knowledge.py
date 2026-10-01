"""Knowledge bases, documents (async ingestion) and search."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from solutionforge.api.deps import RequestMetaDep, SessionDep, TenantDep
from solutionforge.domain.knowledge import Document, DocumentStatus, KnowledgeBase
from solutionforge.retrieval.search import DEFAULT_MIN_DENSE_SCORE, Retriever, Strategy
from solutionforge.services import kb_service as svc

router = APIRouter(prefix="/orgs/{org_id}", tags=["knowledge"])

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
MetaValue = Annotated[str, StringConstraints(max_length=200)]


def _retriever(request: Request) -> Retriever:
    return request.app.state.retriever  # type: ignore[no-any-return]


class CreateKB(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    description: str = Field(default="", max_length=2000)
    chunk_size: int = Field(default=800, ge=200, le=4000)
    chunk_overlap: int = Field(default=120, ge=0, le=1000)


class KBOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    embedder: str
    chunk_size: int
    chunk_overlap: int
    created_at: datetime


class CreateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    content: str = Field(min_length=1, max_length=svc.MAX_DOCUMENT_CHARS)
    metadata: dict[str, MetaValue] = Field(default_factory=dict, max_length=20)
    source_uri: str | None = Field(default=None, max_length=2000)


class DocumentOut(BaseModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    title: str
    source_uri: str | None
    metadata: dict[str, Any]
    status: DocumentStatus
    chunk_count: int
    attempts: int
    last_error: str | None
    created_at: datetime
    ingested_at: datetime | None


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    strategy: Strategy = Strategy.HYBRID
    filters: dict[str, MetaValue] = Field(default_factory=dict, max_length=5)
    min_dense_score: float = Field(default=DEFAULT_MIN_DENSE_SCORE, ge=0, le=1)


def _doc_out(d: Document) -> DocumentOut:
    return DocumentOut(
        id=d.id,
        knowledge_base_id=d.knowledge_base_id,
        title=d.title,
        source_uri=d.source_uri,
        metadata=d.doc_metadata,
        status=d.status,
        chunk_count=d.chunk_count,
        attempts=d.attempts,
        last_error=d.last_error,
        created_at=d.created_at,
        ingested_at=d.ingested_at,
    )


@router.post("/knowledge-bases", response_model=KBOut, status_code=status.HTTP_201_CREATED)
async def create_kb(
    body: CreateKB, request: Request, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> KnowledgeBase:
    return await svc.create_kb(
        session,
        ctx,
        name=body.name,
        description=body.description,
        embedder=_retriever(request).embedder.name,
        chunk_size=body.chunk_size,
        chunk_overlap=body.chunk_overlap,
        request=meta,
    )


@router.get("/knowledge-bases", response_model=list[KBOut])
async def list_kbs(session: SessionDep, ctx: TenantDep) -> list[KnowledgeBase]:
    return await svc.list_kbs(session, ctx)


@router.get("/knowledge-bases/{kb_id}", response_model=KBOut)
async def get_kb(kb_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> KnowledgeBase:
    return await svc.get_kb(session, ctx, kb_id)


@router.delete("/knowledge-bases/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(
    kb_id: uuid.UUID, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> None:
    await svc.delete_kb(session, ctx, kb_id, request=meta)


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "Identical content already present: existing document"}},
)
async def add_document(
    kb_id: uuid.UUID,
    body: CreateDocument,
    response: Response,
    session: SessionDep,
    ctx: TenantDep,
    meta: RequestMetaDep,
) -> DocumentOut:
    doc, created = await svc.add_document(
        session,
        ctx,
        kb_id,
        title=body.title,
        content=body.content,
        metadata=body.metadata,
        source_uri=body.source_uri,
        request=meta,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return _doc_out(doc)


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: uuid.UUID,
    session: SessionDep,
    ctx: TenantDep,
    status_: Annotated[DocumentStatus | None, Query(alias="status")] = None,
) -> list[DocumentOut]:
    return [_doc_out(d) for d in await svc.list_documents(session, ctx, kb_id, status=status_)]


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def get_document(doc_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> DocumentOut:
    return _doc_out(await svc.get_document(session, ctx, doc_id))


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    doc_id: uuid.UUID, session: SessionDep, ctx: TenantDep, meta: RequestMetaDep
) -> None:
    await svc.delete_document(session, ctx, doc_id, request=meta)


@router.post("/documents/{doc_id}/retry", response_model=DocumentOut)
async def retry_document(doc_id: uuid.UUID, session: SessionDep, ctx: TenantDep) -> DocumentOut:
    return _doc_out(await svc.retry_document(session, ctx, doc_id))


@router.post("/knowledge-bases/{kb_id}/search")
async def search(
    kb_id: uuid.UUID, body: SearchIn, request: Request, session: SessionDep, ctx: TenantDep
) -> dict[str, Any]:
    hits = await svc.search(
        session,
        ctx,
        _retriever(request),
        kb_id,
        query=body.query,
        top_k=body.top_k,
        strategy=body.strategy,
        filters=body.filters,
        min_dense_score=body.min_dense_score,
    )
    return {"strategy": body.strategy.value, "results": [svc.hit_dict(h) for h in hits]}
