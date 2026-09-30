"""Deterministic mock provider.

Two modes:

* **Default.** Given an output schema, return the *minimal* JSON value that satisfies it
  (first enum value, ``minimum``, ``minItems`` copies, …); otherwise return a short
  deterministic text derived from the prompt. Same input → same output, so workflows and
  evaluations are reproducible without an API key.
* **Scripted.** Tests (and the evaluation harness) queue replies per prompt substring:
  text, JSON, malformed JSON, refusals, truncation, rate limits, outages, or delays. This is
  how every failure path in the LLM layer is exercised deterministically.

Token usage is estimated at ~4 chars/token so metering and budgets behave realistically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from solutionforge.llm.pricing import estimate_tokens
from solutionforge.llm.types import (
    InvalidRequest,
    ProviderRequest,
    ProviderResponse,
    ProviderUnavailable,
    RateLimited,
    Usage,
)

MOCK_MODELS = frozenset({"mock-1", "mock-fast"})

ReplyKind = Literal[
    "text", "json", "raw", "refusal", "truncated", "rate_limit", "unavailable", "bad_request"
]


@dataclass(frozen=True, slots=True)
class MockReply:
    kind: ReplyKind
    payload: Any = None
    delay_seconds: float = 0.0

    @classmethod
    def text(cls, text: str) -> MockReply:
        return cls("text", text)

    @classmethod
    def json(cls, value: Any) -> MockReply:
        return cls("json", value)

    @classmethod
    def raw(cls, text: str) -> MockReply:
        """Returned verbatim, e.g. malformed JSON."""
        return cls("raw", text)

    @classmethod
    def error(cls, kind: ReplyKind) -> MockReply:
        return cls(kind)

    @classmethod
    def slow(cls, seconds: float, then: MockReply | None = None) -> MockReply:
        base = then or cls.text("slow reply")
        return cls(base.kind, base.payload, delay_seconds=seconds)


@dataclass(slots=True)
class _Rule:
    match: str | None
    replies: deque[MockReply]
    model: str | None = None


@dataclass(slots=True)
class MockCall:
    model: str
    system: str | None
    messages: list[dict[str, str]]
    output_schema: dict[str, Any] | None


@dataclass
class MockProvider:
    name: str = "mock"
    calls: list[MockCall] = field(default_factory=list)
    _rules: list[_Rule] = field(default_factory=list)

    def script(
        self, *replies: MockReply, match: str | None = None, model: str | None = None
    ) -> None:
        """Queue replies for requests whose last user message contains ``match`` (any if
        None) and, optionally, only for ``model``. Consumed in order; then default mode."""
        self._rules.append(_Rule(match, deque(replies), model))

    def reset(self) -> None:
        self.calls.clear()
        self._rules.clear()

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if request.model not in MOCK_MODELS:
            raise InvalidRequest(f"unknown mock model {request.model!r}")
        self.calls.append(
            MockCall(
                request.model,
                request.system,
                [{"role": m.role, "content": m.content} for m in request.messages],
                request.output_schema,
            )
        )
        prompt = request.messages[-1].content if request.messages else ""
        usage_in = estimate_tokens(
            (request.system or "") + "".join(m.content for m in request.messages)
        )

        reply = self._next_scripted(prompt, request.model)
        if reply is None:
            reply = (
                MockReply.json(minimal_instance(request.output_schema))
                if request.output_schema
                else MockReply.text(_default_text(request.model, prompt))
            )
        if reply.delay_seconds:
            await asyncio.sleep(reply.delay_seconds)

        match reply.kind:
            case "rate_limit":
                raise RateLimited("mock rate limit", retry_after=0.0)
            case "unavailable":
                raise ProviderUnavailable("mock provider unavailable")
            case "bad_request":
                raise InvalidRequest("mock bad request")
            case "refusal":
                return self._response("", usage_in, "refusal")
            case "truncated":
                return self._response('{"partial": ', usage_in, "max_tokens")
            case "json":
                return self._response(json.dumps(reply.payload), usage_in, "end_turn")
            case _:
                return self._response(str(reply.payload), usage_in, "end_turn")

    def _next_scripted(self, prompt: str, model: str) -> MockReply | None:
        for rule in self._rules:
            if not rule.replies:
                continue
            if rule.model is not None and rule.model != model:
                continue
            if rule.match is None or rule.match in prompt:
                return rule.replies.popleft()
        return None

    @staticmethod
    def _response(text: str, input_tokens: int, stop: str) -> ProviderResponse:
        usage = Usage(input_tokens=input_tokens, output_tokens=estimate_tokens(text))
        return ProviderResponse(text=text, usage=usage, stop_reason=stop, request_id="mock")


def _default_text(model: str, prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
    return f"[{model}:{digest}] {prompt[:200]}"


def minimal_instance(schema: dict[str, Any] | None, _depth: int = 0) -> Any:
    """Smallest deterministic value satisfying common JSON Schema constructs."""
    if not schema or _depth > 16:
        return None
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for combo in ("anyOf", "oneOf", "allOf"):
        if schema.get(combo):
            return minimal_instance(schema[combo][0], _depth + 1)
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    match t:
        case "object":
            props: dict[str, Any] = schema.get("properties", {})
            return {
                k: minimal_instance(props.get(k, {}), _depth + 1)
                for k in schema.get("required", list(props))
            }
        case "array":
            item = minimal_instance(schema.get("items", {}), _depth + 1)
            return [item] * int(schema.get("minItems", 0))
        case "string":
            return "x" * int(schema.get("minLength", 0)) if schema.get("minLength") else "mock"
        case "integer":
            return int(schema.get("minimum", 0))
        case "number":
            return schema.get("minimum", 0)
        case "boolean":
            return False
        case _:
            return None
