"""Embedding providers.

``HashingEmbedder`` is a deterministic, dependency-free *lexical* embedder (signed feature
hashing of word unigrams and bigrams, sublinear TF, L2-normalised). It captures term overlap,
not meaning: synonyms do not match. It exists so the full RAG path, including pgvector
indexing, runs offline in development, CI and demos. A semantic model (e.g. Voyage, OpenAI,
a local sentence-transformer) plugs in behind the same ``Embedder`` protocol; the measured
retrieval numbers in docs/RETRIEVAL.md are for this lexical embedder and say so.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections import Counter
from typing import Protocol

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
