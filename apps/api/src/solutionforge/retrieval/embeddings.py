"""Embedding providers.

``HashingEmbedder`` is a deterministic, dependency-free *lexical* embedder (signed feature
hashing of word unigrams and bigrams, sublinear TF, L2-normalised). It captures term overlap,
not meaning: synonyms do not match. It exists so the full RAG path, including pgvector
indexing, runs offline in development, CI and demos without downloading a model.

``FastEmbedEmbedder`` is a real semantic sentence-embedding model (default
``BAAI/bge-small-en-v1.5``) for knowledge bases that opt into it. Which embedder a knowledge
base uses is recorded on the KB and resolved through ``EmbedderRegistry``.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import math
import re
import threading
from collections import Counter
from typing import Any, Protocol, cast

EMBEDDING_DIM = 1024  # fixed by the pgvector column; a different model dim needs a migration

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
        "this",
        "these",
        "those",
        "i",
        "you",
        "we",
        "they",
        "he",
        "she",
        "our",
        "your",
        "their",
        "my",
        "me",
    ]
)


class Embedder(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOP]


class HashingEmbedder:
    name = "hashing-v1"
    dim = EMBEDDING_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        features = Counter(tokens) + Counter(f"{a}_{b}" for a, b in itertools.pairwise(tokens))
        vec = [0.0] * self.dim
        for feature, tf in features.items():
            h = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            weight = 1.0 + math.log(tf)
            # Bigrams count half: they sharpen phrase matches without dominating.
            vec[index] += sign * weight * (0.5 if "_" in feature else 1.0)
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    """Vectors are unit-normalised, so the dot product is the cosine similarity."""
    return sum(x * y for x, y in zip(a, b, strict=True))


class FastEmbedEmbedder:
    """Semantic sentence embeddings via ``fastembed`` (ONNX Runtime, no PyTorch).

    Default model: ``BAAI/bge-small-en-v1.5`` (MIT), 384-dim, unit-normalised, compared with
    cosine similarity. bge models expect a short instruction on *queries* only, which
    ``fastembed``'s ``query_embed`` adds; documents use ``embed``.

    Storage: the pgvector column is ``vector(1024)``. Vectors are zero-padded to that width.
    Padding with zeros changes neither dot products nor norms, so cosine similarity (and HNSW
    ordering) is identical to the native 384-dim space (see ``tests/unit/test_embeddings.py``).
    A knowledge base never mixes embedders: its ``embedder`` name selects one at ingest *and*
    query time.

    The model loads lazily on first use (first run downloads ~65 MB to ``cache_dir``) and runs
    in a worker thread so ONNX inference never blocks the event loop.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        *,
        name: str = "bge-small-en-v1.5",
        native_dim: int = 384,
        cache_dir: str | None = None,
    ) -> None:
        self.name = name
        self.dim = EMBEDDING_DIM
        self.native_dim = native_dim
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                try:
                    # Optional dependency ("semantic" extra), imported only on first use.
                    from fastembed import TextEmbedding
                except ImportError as exc:
                    raise SemanticExtraMissing() from exc
                self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        return self._model

    def _pad(self, vec: Any) -> list[float]:
        values = [float(x) for x in vec]
        if len(values) != self.native_dim:
            raise ValueError(
                f"{self.model_name} returned {len(values)} dims, expected {self.native_dim}"
            )
        return values + [0.0] * (self.dim - self.native_dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        def run() -> list[list[float]]:
            return [self._pad(v) for v in self._load().embed(texts)]

        return await asyncio.to_thread(run)

    async def embed_query(self, text: str) -> list[float]:
        def run() -> list[float]:
            return self._pad(next(iter(self._load().query_embed([text]))))

        return await asyncio.to_thread(run)


SEMANTIC_INSTALL_HINT = 'pip install "solutionforge[semantic]" (or -e ".[semantic]")'


class SemanticExtraMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "the semantic embedder needs the optional 'fastembed' package: " + SEMANTIC_INSTALL_HINT
        )


def semantic_extra_installed() -> bool:
    """Whether the optional ``semantic`` extra is importable, without importing it."""
    import importlib.util

    return importlib.util.find_spec("fastembed") is not None


async def embed_query(embedder: Embedder, text: str) -> list[float]:
    """Query-side embedding: asymmetric models (bge) prefix queries; others embed as-is."""
    special = getattr(embedder, "embed_query", None)
    if special is not None:
        return cast(list[float], await special(text))
    [vec] = await embedder.embed([text])
    return vec


class EmbedderRegistry:
    """Embedders by name. A knowledge base records the name it was ingested with, and both
    ingestion and search resolve it here, so a KB can never be queried with a different model
    than the one that indexed it."""

    def __init__(self, default: Embedder, *others: Embedder) -> None:
        self.default = default
        self._by_name: dict[str, Embedder] = {e.name: e for e in (default, *others)}

    def get(self, name: str) -> Embedder:
        try:
            return self._by_name[name]
        except KeyError:
            raise UnknownEmbedder(
                f"knowledge base uses embedder {name!r}, which is not enabled in this process"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._by_name)


class UnknownEmbedder(LookupError):
    pass


def as_registry(embedders: Embedder | EmbedderRegistry) -> EmbedderRegistry:
    return embedders if isinstance(embedders, EmbedderRegistry) else EmbedderRegistry(embedders)
