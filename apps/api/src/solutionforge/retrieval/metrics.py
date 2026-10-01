"""Retrieval quality metrics (document-level relevance)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def dedupe(seq: Sequence[str]) -> list[str]:
    """Collapse chunk-level hits to document order (first occurrence wins)."""
    seen: set[str] = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(dedupe(ranked)[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for i, doc in enumerate(dedupe(ranked), start=1):
        if doc in relevant:
            return 1.0 / i
    return 0.0


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    strategy: str
    queries: int
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float

    def row(self) -> str:
        return (
            f"| {self.strategy} | {self.queries} | {self.recall_at_1:.3f} | "
            f"{self.recall_at_3:.3f} | {self.recall_at_5:.3f} | {self.mrr:.3f} |"
        )


def summarize(strategy: str, results: list[tuple[list[str], set[str]]]) -> RetrievalReport:
    n = len(results) or 1
    return RetrievalReport(
        strategy=strategy,
        queries=len(results),
        recall_at_1=sum(recall_at_k(r, rel, 1) for r, rel in results) / n,
        recall_at_3=sum(recall_at_k(r, rel, 3) for r, rel in results) / n,
        recall_at_5=sum(recall_at_k(r, rel, 5) for r, rel in results) / n,
        mrr=sum(reciprocal_rank(r, rel) for r, rel in results) / n,
    )
