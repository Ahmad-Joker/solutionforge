"""Native tool calling through the real Anthropic SDK against canned HTTP responses, plus
the pricing rules the assistant's cost accounting depends on.

No network and no API key: ``httpx2.MockTransport`` stands in for api.anthropic.com.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from solutionforge.llm.pricing import DEFAULT_PRICES, ModelPrice
from solutionforge.llm.providers.claude import AnthropicProvider, usage_from_sdk
from solutionforge.llm.types import (
    ConverseRequest,
    ProviderUnavailable,
    RateLimited,
    ToolSpec,
    Usage,
)

SEARCH = ToolSpec(
    name="search_documents",
    description="Search the help-centre documents.",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)


def tool_use_body() -> dict[str, Any]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [
            {"type": "text", "text": "Let me look that up."},
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "search_documents",
                "input": {"query": "refund window"},
            },
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 700,
            "output_tokens": 40,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 300,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 100,
                "ephemeral_1h_input_tokens": 200,
            },
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


async def test_tools_are_sent_and_tool_use_blocks_parsed() -> None:
    p, seen = provider(
        lambda r: httpx2.Response(200, json=tool_use_body(), headers={"request-id": "req_9"})
    )
    resp = await p.converse(
        ConverseRequest(
            model="claude-haiku-5-5",
            messages=({"role": "user", "content": "How long do refunds take?"},),
            system="You are a support assistant.",
            tools=(SEARCH,),
            tool_choice={"type": "auto"},
            max_tokens=512,
        )
    )
    body = json.loads(seen[0].content)
    assert body["tools"] == [
        {
            "name": "search_documents",
            "description": "Search the help-centre documents.",
            "input_schema": SEARCH.input_schema,
        }
    ]
    assert body["tool_choice"] == {"type": "auto"}
    assert resp.stop_reason == "tool_use"
    assert [(c.id, c.name, c.input) for c in resp.tool_calls] == [
        ("toolu_01", "search_documents", {"query": "refund window"})
    ]
    assert resp.content[1] == {
        "type": "tool_use",
        "id": "toolu_01",
        "name": "search_documents",
        "input": {"query": "refund window"},
    }
    assert resp.text == "Let me look that up."
    assert resp.usage == Usage(700, 40, 0, 100, 200)
    assert resp.request_id == "req_9"


async def test_tool_result_history_round_trips_unchanged() -> None:
    final = tool_use_body() | {
        "content": [{"type": "text", "text": "Refunds take 5 business days [doc:refunds#0]."}],
        "stop_reason": "end_turn",
    }
    p, seen = provider(lambda r: httpx2.Response(200, json=final))
    first = tool_use_body()["content"]
    history = (
        {"role": "user", "content": "How long do refunds take?"},
        {"role": "assistant", "content": first},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "<doc>...</doc>"}
            ],
        },
    )
    resp = await p.converse(ConverseRequest(model="m", messages=history, tools=(SEARCH,)))
    assert json.loads(seen[0].content)["messages"] == [dict(m) for m in history]
    assert resp.tool_calls == ()
    assert resp.stop_reason == "end_turn"


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, RateLimited), (529, ProviderUnavailable), (500, ProviderUnavailable)],
)
async def test_converse_maps_transient_errors(status: int, error: type[Exception]) -> None:
    p, _ = provider(
        lambda r: httpx2.Response(
            status, json={"type": "error", "error": {"type": "x", "message": "busy"}}
        )
    )
    with pytest.raises(error):
        await p.converse(ConverseRequest(model="m", messages=({"role": "user", "content": "hi"},)))


def test_usage_without_ttl_breakdown_counts_writes_as_5m() -> None:
    u = SimpleNamespace(
        input_tokens=10,
        output_tokens=2,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=40,
        cache_creation=None,
    )
    assert usage_from_sdk(u) == Usage(10, 2, 0, 40, 0)


def test_one_hour_cache_writes_priced_separately() -> None:
    sonnet = DEFAULT_PRICES["anthropic:claude-sonnet-5-5"]
    # 1M of each category, so the micro-dollar result equals the $/MTok list price x 1e6.
    assert sonnet.cost_micro_usd(Usage(cache_write_1h_tokens=1_000_000)) == 4_000_000
    assert sonnet.cost_micro_usd(Usage(cache_write_tokens=1_000_000)) == 2_500_000
    assert sonnet.cost_micro_usd(Usage(cache_read_tokens=1_000_000)) == 100_000
    # Unlisted 1h price defaults to 2x input, per the published multiplier.
    assert (
        ModelPrice.of("3", "15", "0.3", "3.75").cost_micro_usd(
            Usage(cache_write_1h_tokens=1_000_000)
        )
        == 6_000_000
    )


def test_haiku_5_5_long_context_tier_applies_to_whole_request() -> None:
    haiku = DEFAULT_PRICES["anthropic:claude-haiku-5-5"]
    short = Usage(input_tokens=100_000, output_tokens=1_000)
    # Prompt length includes cache reads and writes: 1 more cached token crosses 100k.
    long = Usage(input_tokens=100_000, output_tokens=1_000, cache_read_tokens=1)
    assert haiku.cost_micro_usd(short) == int(Decimal("0.10") * 100_000 + Decimal("0.50") * 1_000)
    assert haiku.cost_micro_usd(long) == int(
        (Decimal("0.50") * 100_000 + Decimal("2.50") * 1_000 + Decimal("0.05")).to_integral_value(
            rounding="ROUND_CEILING"
        )
    )


def test_current_model_list_prices() -> None:
    def per_mtok(ref: str) -> tuple[Decimal, Decimal]:
        p = DEFAULT_PRICES[ref]
        return p.input, p.output

    assert per_mtok("anthropic:claude-opus-5-5") == (Decimal(4), Decimal(20))
    assert per_mtok("anthropic:claude-sonnet-5-5") == (Decimal(2), Decimal(10))
    assert per_mtok("anthropic:claude-haiku-5-5") == (Decimal("0.10"), Decimal("0.50"))
