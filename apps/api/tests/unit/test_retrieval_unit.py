from __future__ import annotations

import itertools
import math
import uuid

import pytest
from jsonschema import Draft202012Validator

from solutionforge.retrieval.chunking import chunk_text
from solutionforge.retrieval.embeddings import EMBEDDING_DIM, HashingEmbedder, cosine, tokenize
from solutionforge.retrieval.metrics import recall_at_k, reciprocal_rank, summarize
from solutionforge.retrieval.search import _rrf, bm25_rank
from solutionforge.workflows.steps.rag import answer_schema, verify_citations

DOC = (
    "Intro paragraph before any heading.\n\n# Refund policy\n\n"
    + " ".join(f"Sentence {i} explains refunds and returns within thirty days." for i in range(30))
    + "\n\n## Shipping\n\nOrders ship in two days. Delays happen during holidays.\n\n"
    + "x" * 950  # one giant 'sentence' that must be hard-wrapped
)

# ------------------------------------------------------------------ chunking


@pytest.mark.parametrize(("size", "overlap"), [(200, 0), (300, 80), (800, 120)])
def test_chunks_cover_document_with_exact_offsets(size: int, overlap: int) -> None:
    chunks = chunk_text(DOC, size=size, overlap=overlap)
    covered: set[int] = set()
    for c in chunks:
        assert DOC[c.char_start : c.char_end].strip() == c.text  # offsets are exact
        assert c.char_end - c.char_start <= size  # never over budget
        covered.update(range(c.char_start, c.char_end))
    assert all(i in covered for i, ch in enumerate(DOC) if not ch.isspace())  # nothing lost
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_chunks_never_cross_headings_and_carry_sections() -> None:
    chunks = chunk_text(DOC, size=300, overlap=80)
    assert chunks[0].section is None  # intro before the first heading
    for c in chunks:
        assert c.text.count("# ") <= 1 and ("## Shipping" not in c.text or c.section == "Shipping")
    assert {c.section for c in chunks} == {None, "Refund policy", "Shipping"}


def test_overlap_repeats_context() -> None:
    chunks = [c for c in chunk_text(DOC, size=300, overlap=120) if c.section == "Refund policy"]
    assert any(a.char_end > b.char_start for a, b in itertools.pairwise(chunks))


@pytest.mark.parametrize(("size", "overlap"), [(50, 0), (300, 200), (300, -1)])
def test_invalid_chunking_params(size: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="require"):
        chunk_text(DOC, size=size, overlap=overlap)


# ------------------------------------------------------------------ embeddings


def test_embedder_is_deterministic_normalised_and_lexical() -> None:
    e = HashingEmbedder()
    a = e.embed_one("refund to my credit card")
    assert a == e.embed_one("refund to my credit card")
    assert len(a) == EMBEDDING_DIM and math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)
    related = cosine(a, e.embed_one("how long does a card refund take"))
    unrelated = cosine(a, e.embed_one("reset your password link"))
    assert related > unrelated
    assert e.embed_one("") == [0.0] * EMBEDDING_DIM
    assert "the" not in tokenize("The refund")


# ------------------------------------------------------------------ ranking


def test_bm25_prefers_rare_matching_terms() -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    docs = [
        (ids[0], "refund policy refund days"),
        (ids[1], "shipping policy days"),
        (ids[2], "password reset"),
    ]
    ranked = bm25_rank("refund days", docs)
    assert ranked[0][0] == ids[0] and ids[2] not in [r[0] for r in ranked]
    assert bm25_rank("the and of", docs) == []  # stopword-only query


def test_rrf_rewards_agreement() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fused = _rrf({"dense": [(a, 0.9), (b, 0.8)], "keyword": [(b, 5.0), (c, 4.0)]})
    assert fused[0].chunk_id == b  # in both lists
    assert fused[0].sources == {"dense": 2, "keyword": 1}


def test_metrics() -> None:
    assert recall_at_k(["a", "a", "b", "c"], {"b", "c"}, 2) == 0.5
    assert recall_at_k(["a", "a", "b", "c"], {"b", "c"}, 3) == 1.0  # chunk dupes collapse
    assert reciprocal_rank(["x", "y", "b"], {"b"}) == pytest.approx(1 / 3)
    rep = summarize("dense", [(["a"], {"a"}), (["b"], {"a"})])
    assert rep.recall_at_1 == 0.5 and rep.mrr == 0.5


# ------------------------------------------------------------------ citations


LABELS = ["S1", "S2", "S3"]


@pytest.mark.parametrize(
    ("result", "ok"),
    [
        (
            {
                "answer": "Refunds take 5-10 days [S1].",
                "citations": ["S1"],
                "insufficient_context": False,
            },
            True,
        ),
        ({"answer": "Not covered.", "citations": [], "insufficient_context": True}, True),
        ({"answer": "Refunds take 5 days.", "citations": [], "insufficient_context": False}, False),
        ({"answer": "Per policy [S7].", "citations": ["S1"], "insufficient_context": False}, False),
        ({"answer": "Per policy [S2].", "citations": ["S1"], "insufficient_context": False}, False),
        ({"answer": "x", "citations": ["S9"], "insufficient_context": False}, False),
    ],
)
def test_verify_citations(result: dict, ok: bool) -> None:  # type: ignore[type-arg]
    assert (verify_citations(result, LABELS) == []) is ok


def test_answer_schema_only_allows_provided_labels() -> None:
    v = Draft202012Validator(answer_schema(LABELS))
    assert v.is_valid({"answer": "a", "citations": ["S1"], "insufficient_context": False})
    assert not v.is_valid({"answer": "a", "citations": ["S4"], "insufficient_context": False})
    assert not v.is_valid({"answer": "a", "citations": ["S1", "S1"], "insufficient_context": False})
