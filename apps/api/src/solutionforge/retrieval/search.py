"""Dense, keyword and hybrid retrieval with metadata filters.

| strategy | PostgreSQL (production)                        | other dialects (local tests)   |
|----------|------------------------------------------------|--------------------------------|
| dense    | pgvector cosine distance ``<=>`` (HNSW index)  | exact cosine scan in Python    |
| keyword  | ``websearch_to_tsquery`` + ``ts_rank_cd`` (GIN index) | Okapi BM25 in Python    |
| hybrid   | Reciprocal Rank Fusion (k=60) of the two lists above, on either backend        |

Both backends apply tenant + knowledge-base scoping and metadata filters in SQL. Rankings
from the two keyword backends are not identical (``ts_rank_cd`` is not BM25); the retrieval
benchmark states which backend produced its numbers.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from solutionforge.domain.knowledge import Chunk, Document
from solutionforge.retrieval.embeddings import EMBEDDING_DIM, Embedder, cosine, tokenize

FILTER_KEY = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
MAX_SCAN_CHUNKS = 50_000  # portable path only; production uses the HNSW index
RRF_K = 60
# Dense hits below this cosine similarity are dropped before ranking/fusion. Without a floor,
# nearest-neighbour search always returns *something*, so an off-topic question would still
# feed unrelated passages to the answer step. Calibrated for HashingEmbedder on the support
# fixture: 0.08 keeps dense recall@5 = 1.0 while off-topic probes peak at 0.078 (thin margin;
# see docs/RETRIEVAL.md). The value is embedder-specific: re-measure when changing embedders.
DEFAULT_MIN_DENSE_SCORE = 0.08


class Strategy(StrEnum):
    DENSE = "dense"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    ordinal: int
    section: str | None
    text: str
    char_start: int
    char_end: int
    score: float
    sources: dict[str, int] = field(default_factory=dict)  # strategy -> rank (1-based)


class Retriever:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self.sessionmaker = sessionmaker
        self.embedder = embedder

    async def search(
        self,
        *,
        organization_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        query: str,
        top_k: int = 5,
        strategy: Strategy = Strategy.HYBRID,
        filters: dict[str, str] | None = None,
        min_dense_score: float = DEFAULT_MIN_DENSE_SCORE,
    ) -> list[SearchHit]:
        if not query.strip():
            return []
        for key in filters or {}:
            if not FILTER_KEY.match(key):
                raise ValueError(f"invalid filter key {key!r}")
        scope = [
            Chunk.organization_id == organization_id,
            Chunk.knowledge_base_id == knowledge_base_id,
            *[Chunk.chunk_metadata[k].as_string() == v for k, v in (filters or {}).items()],
        ]
        async with self.sessionmaker() as s:
            pg = s.get_bind().dialect.name == "postgresql"
            if strategy == Strategy.HYBRID:
                pool = max(top_k * 4, 20)
                dense = await self._dense(s, scope, query, pool, pg, min_dense_score)
                keyword = await self._keyword(s, scope, query, pool, pg)
                fused = _rrf({"dense": dense, "keyword": keyword})[:top_k]
            else:
                single = (
                    await self._dense(s, scope, query, top_k, pg, min_dense_score)
                    if strategy == Strategy.DENSE
                    else await self._keyword(s, scope, query, top_k, pg)
                )
                fused = [
                    _Fused(cid, score, {strategy.value: rank})
                    for rank, (cid, score) in enumerate(single, start=1)
                ]
            return await self._hydrate(s, fused)

    # ------------------------------------------------------------------ strategies

    async def _dense(
        self, s: AsyncSession, scope: list[Any], query: str, k: int, pg: bool, min_score: float
    ) -> list[tuple[uuid.UUID, float]]:
        [qvec] = await self.embedder.embed([query])
        if pg:
            from pgvector.sqlalchemy import Vector

            distance = Chunk.embedding.op("<=>", return_type=sa.Float)(
                sa.bindparam("query_vec", qvec, type_=Vector(EMBEDDING_DIM))
            )
            rows = await s.execute(
                sa.select(Chunk.id, distance).where(*scope).order_by(distance).limit(k)
            )
            return [(cid, 1.0 - float(d)) for cid, d in rows if 1.0 - float(d) >= min_score]
        rows = await s.execute(
            sa.select(Chunk.id, Chunk.embedding).where(*scope).limit(MAX_SCAN_CHUNKS)
        )
        scored = [(cid, cosine(qvec, emb)) for cid, emb in rows]
        scored.sort(key=lambda x: (-x[1], str(x[0])))
        return [x for x in scored[:k] if x[1] >= min_score]

    async def _keyword(
        self, s: AsyncSession, scope: list[Any], query: str, k: int, pg: bool
    ) -> list[tuple[uuid.UUID, float]]:
        if pg:
            # websearch_to_tsquery ANDs every term, so a natural-language question only
            # matches chunks containing *all* of its words (measured: recall@5 0.58 on the
            # benchmark). Keep its safe parsing (stemming, stop words, phrases, negation) but
            # OR the terms, like BM25 does, and let ts_rank_cd reward chunks covering more.
            parsed = sa.cast(sa.func.websearch_to_tsquery("english", query), sa.Text)
            tsq = sa.cast(sa.func.replace(parsed, " & ", " | "), TSQUERY)
            tsv = sa.func.to_tsvector("english", Chunk.text)
            rank = sa.func.ts_rank_cd(tsv, tsq)
            rows = await s.execute(
                sa.select(Chunk.id, rank)
                .where(*scope, tsv.op("@@")(tsq))
                .order_by(rank.desc(), Chunk.id)
                .limit(k)
            )
            return [(cid, float(r)) for cid, r in rows]
        texts = await s.execute(
            sa.select(Chunk.id, Chunk.text).where(*scope).limit(MAX_SCAN_CHUNKS)
        )
        return bm25_rank(query, [(cid, text) for cid, text in texts])[:k]

    async def _hydrate(self, s: AsyncSession, ranked: list[_Fused]) -> list[SearchHit]:
        if not ranked:
            return []
        rows = await s.execute(
            sa.select(Chunk, Document.title)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.id.in_([r.chunk_id for r in ranked]))
        )
        by_id = {c.id: (c, title) for c, title in rows}
        hits = []
        for r in ranked:
            if r.chunk_id not in by_id:  # deleted between ranking and hydration
                continue
            c, title = by_id[r.chunk_id]
            hits.append(
                SearchHit(
                    chunk_id=c.id,
                    document_id=c.document_id,
                    document_title=title,
                    ordinal=c.ordinal,
                    section=c.section,
                    text=c.text,
                    char_start=c.char_start,
                    char_end=c.char_end,
                    score=round(r.score, 6),
                    sources=r.sources,
                )
            )
        return hits


def bm25_rank(
    query: str, docs: list[tuple[uuid.UUID, str]], *, k1: float = 1.2, b: float = 0.75
) -> list[tuple[uuid.UUID, float]]:
    """Okapi BM25 over the candidate set (portable keyword backend)."""
    q_terms = set(tokenize(query))
    if not q_terms or not docs:
        return []
    tokenized = [(cid, Counter(tokenize(text))) for cid, text in docs]
    n = len(tokenized)
    avgdl = sum(sum(tf.values()) for _, tf in tokenized) / n or 1.0
    df = Counter(t for _, tf in tokenized for t in q_terms if t in tf)
    scored = []
    for cid, tf in tokenized:
        dl = sum(tf.values())
        score = 0.0
        for t in q_terms:
            f = tf.get(t, 0)
            if f:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scored.append((cid, score))
    scored.sort(key=lambda x: (-x[1], str(x[0])))
    return scored


@dataclass
class _Fused:
    chunk_id: uuid.UUID
    score: float
    sources: dict[str, int]


def _rrf(lists: dict[str, list[tuple[uuid.UUID, float]]]) -> list[_Fused]:
    """Reciprocal Rank Fusion: score = Σ 1/(k + rank). Robust to incomparable score scales."""
    fused: dict[uuid.UUID, _Fused] = {}
    for name, ranked in lists.items():
        for rank, (cid, _) in enumerate(ranked, start=1):
            f = fused.setdefault(cid, _Fused(cid, 0.0, {}))
            f.score += 1.0 / (RRF_K + rank)
            f.sources[name] = rank
    return sorted(fused.values(), key=lambda f: (-f.score, str(f.chunk_id)))
