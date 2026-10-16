"""Embedders: semantic-embedder logic with a fake model (no download), the registry, and the
optional-extra contract. These run identically with and without the ``semantic`` extra."""

from __future__ import annotations

import math
import subprocess
import sys
from collections.abc import Iterable, Iterator

import pytest

from solutionforge.core.config import Settings
from solutionforge.retrieval import embeddings as emb
from solutionforge.retrieval.embeddings import (
    EMBEDDING_DIM,
    EmbedderRegistry,
    FastEmbedEmbedder,
    HashingEmbedder,
    SemanticExtraMissing,
    UnknownEmbedder,
    as_registry,
    embed_query,
)
from solutionforge.retrieval.factory import build_embedders


class FakeModel:
    """Stands in for fastembed.TextEmbedding: 4-dim vectors, distinct query path."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, texts: Iterable[str]) -> Iterator[list[float]]:
        self.calls.append("embed")
        for t in texts:
            yield [float(len(t)), 1.0, 0.0, 2.0]

    def query_embed(self, texts: Iterable[str]) -> Iterator[list[float]]:
        self.calls.append("query_embed")
        for _ in texts:
            yield [1.0, 2.0, 3.0, 4.0]


def fake_embedder() -> tuple[FastEmbedEmbedder, FakeModel]:
    e = FastEmbedEmbedder("fake/model", name="fake", native_dim=4)
    model = FakeModel()
    e._model = model  # skip the lazy load (and any download)
    return e, model


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def test_vectors_are_zero_padded_without_changing_cosine() -> None:
    e, _ = fake_embedder()
    [a, b] = await e.embed(["short", "a longer text"])
    assert len(a) == len(b) == EMBEDDING_DIM
    assert a[4:] == [0.0] * (EMBEDDING_DIM - 4)
    native_a, native_b = [5.0, 1.0, 0.0, 2.0], [13.0, 1.0, 0.0, 2.0]
    assert cosine(a, b) == pytest.approx(cosine(native_a, native_b))


async def test_queries_use_the_query_path() -> None:
    e, model = fake_embedder()
    q = await embed_query(e, "refund window")
    assert q[:4] == [1.0, 2.0, 3.0, 4.0] and model.calls == ["query_embed"]
    h = HashingEmbedder()  # symmetric embedders fall back to embed()
    assert await embed_query(h, "refund") == (await h.embed(["refund"]))[0]


async def test_wrong_model_dimension_is_rejected() -> None:
    e = FastEmbedEmbedder("fake/model", name="fake", native_dim=8)
    e._model = FakeModel()  # returns 4 dims
    with pytest.raises(ValueError, match="returned 4 dims, expected 8"):
        await e.embed(["x"])


def test_registry() -> None:
    h = HashingEmbedder()
    e, _ = fake_embedder()
    reg = EmbedderRegistry(h, e)
    assert (
        reg.default is h and reg.get("fake") is e and sorted(reg.names()) == ["fake", "hashing-v1"]
    )
    with pytest.raises(UnknownEmbedder):
        reg.get("nope")
    assert as_registry(reg) is reg and as_registry(h).default is h


def test_base_install_builds_only_the_lexical_embedder() -> None:
    assert build_embedders(Settings()).names() == ["hashing-v1"]
    assert build_embedders(None).default.name == "hashing-v1"


def test_enabling_semantic_without_the_extra_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(emb, "semantic_extra_installed", lambda: False)
    monkeypatch.setattr("solutionforge.retrieval.factory.semantic_extra_installed", lambda: False)
    with pytest.raises(SemanticExtraMissing, match=r"\[semantic\]"):
        build_embedders(Settings(semantic_embedder_enabled=True))


def test_enabling_semantic_with_the_extra_registers_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("solutionforge.retrieval.factory.semantic_extra_installed", lambda: True)
    reg = build_embedders(Settings(semantic_embedder_enabled=True))
    assert sorted(reg.names()) == ["bge-small-en-v1.5", "hashing-v1"]  # loaded lazily, not yet
    with pytest.raises(ValueError, match="not enabled"):
        build_embedders(Settings(default_embedder="bge-small-en-v1.5"))


async def test_lazy_load_reports_a_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fastembed", None)  # makes `import fastembed` fail
    e = FastEmbedEmbedder()
    with pytest.raises(SemanticExtraMissing):
        await e.embed(["x"])


def test_app_import_does_not_import_fastembed() -> None:
    """The base install must not need the extra: building the app never imports it."""
    code = (
        "import sys\n"
        "from solutionforge.core.config import Environment, Settings\n"
        "from solutionforge.main import create_app\n"
        "create_app(Settings(environment=Environment.TEST, database_url='sqlite+aiosqlite:///unused.db'))\n"
        "assert 'fastembed' not in sys.modules, 'fastembed imported eagerly'\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)  # noqa: S603
    assert r.returncode == 0, r.stderr[-2000:]


def test_semantic_extra_detection_matches_reality() -> None:
    import importlib.util

    assert emb.semantic_extra_installed() == (importlib.util.find_spec("fastembed") is not None)
