"""Anthropic adapter exercised through the real SDK against canned HTTP responses.

No network and no API key: an ``httpx2.MockTransport`` stands in for api.anthropic.com. This
verifies request construction, response/usage mapping and error classification exactly as
the SDK surfaces them.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2
import pytest

from solutionforge.llm.providers.claude import AnthropicProvider
from solutionforge.llm.types import (
    InvalidRequest,
    Message,
    ProviderAuthError,
    ProviderRequest,
    ProviderUnavailable,
    RateLimited,
)

REQ = ProviderRequest(
    model="claude-opus-5",
    messages=(Message("user", "Classify: refund please"),),
    system="You are a classifier.",
    max_tokens=256,
    output_schema={
        "type": "object",
        "properties": {"intent": {"type": "string"}},
        "required": ["intent"],
        "additionalProperties": False,
    },
    effort="low",
)


def message_body(text: str = '{"intent":"refund"}', stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 9,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 50,
        },
    }


def provider(handler: Any) -> tuple[AnthropicProvider, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def wrapped(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test-not-a-real-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(wrapped)),
    )
    return AnthropicProvider(client=client), seen


async def test_request_and_response_mapping() -> None:
    p, seen = provider(
        lambda r: httpx2.Response(200, json=message_body(), headers={"request-id": "req_123"})
    )
    resp = await p.complete(REQ)

    body = json.loads(seen[0].content)
    assert body["model"] == "claude-opus-5"
    assert body["system"] == "You are a classifier."
    assert body["messages"] == [{"role": "user", "content": "Classify: refund please"}]
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": REQ.output_schema},
        "effort": "low",
    }
    assert "temperature" not in body  # sampling params are rejected on current models

    assert resp.text == '{"intent":"refund"}'
    assert resp.stop_reason == "end_turn"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (120, 9)
    assert (resp.usage.cache_read_tokens, resp.usage.cache_write_tokens) == (1000, 50)
    assert resp.request_id == "req_123"


async def test_refusal_stop_reason_is_surfaced() -> None:
    p, _ = provider(lambda r: httpx2.Response(200, json=message_body("", "refusal")))
    assert (await p.complete(REQ)).stop_reason == "refusal"


def _error(status: int, etype: str, headers: dict[str, str] | None = None) -> Any:
    return lambda r: httpx2.Response(
        status,
        json={"type": "error", "error": {"type": etype, "message": "x"}},
        headers=headers or {},
    )


@pytest.mark.parametrize(
    ("status", "etype", "expected", "retryable"),
    [
        (429, "rate_limit_error", RateLimited, True),
        (529, "overloaded_error", ProviderUnavailable, True),
        (500, "api_error", ProviderUnavailable, True),
        (400, "invalid_request_error", InvalidRequest, False),
        (404, "not_found_error", InvalidRequest, False),
        (401, "authentication_error", ProviderAuthError, False),
        (403, "permission_error", ProviderAuthError, False),
    ],
)
async def test_error_classification_and_no_sdk_retries(
    status: int, etype: str, expected: type, retryable: bool
) -> None:
    p, seen = provider(_error(status, etype, {"retry-after": "7"}))
    with pytest.raises(expected) as ei:
        await p.complete(REQ)
    assert ei.value.retryable is retryable
    assert len(seen) == 1  # the SDK did not retry behind our back
    if expected is RateLimited:
        assert ei.value.retry_after == 7.0


async def test_connection_failure_is_transient() -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    p, _ = provider(boom)
    with pytest.raises(ProviderUnavailable):
        await p.complete(REQ)
