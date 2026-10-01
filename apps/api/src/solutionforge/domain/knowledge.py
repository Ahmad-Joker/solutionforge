"""Knowledge bases, documents (with ingestion job state) and embedded chunks."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from solutionforge.core.clock import utcnow
from solutionforge.db.base import (
    Base,
    Embedding,
    JSONType,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from solutionforge.retrieval.embeddings import EMBEDDING_DIM


class DocumentStatus(StrEnum):
    PENDING = "pending"  # queued for ingestion (or waiting for a retry)
    INGESTING = "ingesting"  # leased by a worker
    READY = "ready"
    FAILED = "failed"  # dead-lettered after max attempts; retry via API


class KnowledgeBase(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (sa.UniqueConstraint("organization_id", "name"),)

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str] = mapped_column(sa.String(2000), nullable=False, default="")
    embedder: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    chunk_size: Mapped[int] = mapped_column(nullable=False, default=800)
    chunk_overlap: Mapped[int] = mapped_column(nullable=False, default=120)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Document(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Same content uploaded twice to one KB is the same document (idempotent upload).
        sa.UniqueConstraint("knowledge_base_id", "content_hash"),
        sa.Index("ix_documents_claim", "status", "next_attempt_at"),
    )

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(sa.String(2000), nullable=True)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    status: Mapped[DocumentStatus] = mapped_column(
        sa.Enum(
            DocumentStatus,
            name="document_status",
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
        ),
        nullable=False,
    )
    chunk_count: Mapped[int] = mapped_column(nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(sa.String(2000), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ingested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Chunk(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        sa.UniqueConstraint("document_id", "ordinal"),
        sa.Index("ix_chunks_kb", "knowledge_base_id"),
    )

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ordinal: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    char_start: Mapped[int] = mapped_column(nullable=False)
    char_end: Mapped[int] = mapped_column(nullable=False)
    section: Mapped[str | None] = mapped_column(sa.String(300), nullable=True)
    # Document metadata copied onto each chunk so filters need no join.
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    embedding: Mapped[list[float]] = mapped_column(Embedding(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
