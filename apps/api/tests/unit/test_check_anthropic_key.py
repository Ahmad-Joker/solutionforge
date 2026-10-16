"""scripts/check_anthropic_key.py: the free key check, through the real SDK, no network."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_anthropic_key.py"


def load() -> Any:
    spec = importlib.util.spec_from_file_location("check_anthropic_key", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def patch(monkeypatch: pytest.MonkeyPatch, mod: Any, handler: Any) -> list[str]:
    paths: list[str] = []
    real = anthropic.Anthropic

    def fake(**kw: Any) -> anthropic.Anthropic:
        def wrapped(r: httpx2.Request) -> httpx2.Response:
            paths.append(f"{r.method} {r.url.path}")
            return handler(r)

        return real(**kw, http_client=httpx2.Client(transport=httpx2.MockTransport(wrapped)))

    monkeypatch.setattr(mod.anthropic, "Anthropic", fake)
    monkeypatch.setenv("SF_ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    return paths


def model(r: httpx2.Request) -> httpx2.Response:
    mid = r.url.path.rsplit("/", 1)[-1]
    if mid == "claude-unknown":
        return httpx2.Response(
            404, json={"type": "error", "error": {"type": "not_found_error", "message": "no"}}
        )
    return httpx2.Response(
        200,
        json={
            "id": mid,
            "type": "model",
            "display_name": mid,
            "created_at": "2026-01-01T00:00:00Z",
        },
    )


def test_valid_key_and_priced_models(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load()
    paths = patch(monkeypatch, mod, model)
    assert mod.main(["anthropic:claude-haiku-5-5", "anthropic:claude-sonnet-5-5"]) == 0
    assert paths == [
        "GET /v1/models/claude-haiku-5-5",
        "GET /v1/models/claude-sonnet-5-5",
    ]  # no /v1/messages
    out = capsys.readouterr().out
    assert "sk-ant" not in out and out.count("price configured: yes") == 2


def test_unknown_or_unpriced_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load()
    patch(monkeypatch, mod, model)
    assert mod.main(["anthropic:claude-unknown"]) == 1
    assert mod.main(["anthropic:claude-unpriced-model"]) == 1  # exists, but no price


def test_rejected_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    mod = load()
    patch(
        monkeypatch,
        mod,
        lambda r: httpx2.Response(
            401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}}
        ),
    )
    assert mod.main([]) == 3 and "KEY REJECTED" in capsys.readouterr().out


def test_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load()
    monkeypatch.setenv("SF_ANTHROPIC_API_KEY", "")
    monkeypatch.chdir(SCRIPT.parent)  # no .env here
    assert mod.main([]) == 2
