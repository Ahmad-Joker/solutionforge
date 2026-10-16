"""Build the process-wide Retriever and its embedders."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.core.config import Settings
from solutionforge.retrieval.embeddings import (
    Embedder,
    EmbedderRegistry,
    FastEmbedEmbedder,
    HashingEmbedder,
    SemanticExtraMissing,
    semantic_extra_installed,
)
from solutionforge.retrieval.search import Retriever


def build_embedders(settings: Settings | None = None) -> EmbedderRegistry:
    """``hashing-v1`` is always available (offline, no download). The semantic embedder is
    enabled with ``SF_SEMANTIC_EMBEDDER_ENABLED=true``; ``SF_DEFAULT_EMBEDDER`` picks which one
    new knowledge bases use."""
    available: list[Embedder] = [HashingEmbedder()]
    if settings is not None and settings.semantic_embedder_enabled:
        if not semantic_extra_installed():
            # Fail at startup, not on the first document or query.
            raise SemanticExtraMissing()
        available.append(
            FastEmbedEmbedder(
                settings.semantic_embedding_model,
                name=settings.semantic_embedder_name,
                native_dim=settings.semantic_embedding_dim,
                cache_dir=settings.embedding_cache_dir,
            )
        )
    by_name = {e.name: e for e in available}
    default_name = settings.default_embedder if settings is not None else "hashing-v1"
    if default_name not in by_name:
        raise ValueError(
            f"SF_DEFAULT_EMBEDDER={default_name!r} is not enabled (available: {sorted(by_name)})"
        )
    default = by_name.pop(default_name)
    return EmbedderRegistry(default, *by_name.values())


def build_retriever(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings | None = None
) -> Retriever:
    return Retriever(sessionmaker, build_embedders(settings))
