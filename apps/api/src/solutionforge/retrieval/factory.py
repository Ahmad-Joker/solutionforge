"""Build the process-wide Retriever."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.retrieval.embeddings import HashingEmbedder
from solutionforge.retrieval.search import Retriever


def build_retriever(sessionmaker: async_sessionmaker[AsyncSession]) -> Retriever:
    return Retriever(sessionmaker, HashingEmbedder())
