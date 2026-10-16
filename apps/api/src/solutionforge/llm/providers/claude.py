"""Anthropic (Claude) adapter using the official ``anthropic`` SDK.

This is the only module that imports the SDK. SDK retries are disabled
(``max_retries=0``): :class:`~solutionforge.llm.service.LLMService` owns retry, fallback and
metering, so every attempt is visible and billed exactly once in our ledger.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

import anthropic

from solutionforge.llm.types import (
    ConverseRequest,
    ConverseResponse,
    InvalidRequest,
    ProviderAuthError,
    ProviderRequest,
    ProviderResponse,
    ProviderUnavailable,
    RateLimited,
    ToolCall,
    Usage,
)

DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self, *, api_key: str | None = None, client: anthropic.AsyncAnthropic | None = None
    ) -> None:
        # Credentials resolve from the environment when api_key is None (SDK default chain).
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.system:
            params["system"] = request.system
        output_config: dict[str, Any] = {}
        if request.output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.output_schema}
        if request.effort is not None:
            output_config["effort"] = request.effort
        if output_config:
            params["output_config"] = output_config

        message = await _call(
            self._client.with_options(
                timeout=request.timeout_seconds, max_retries=0
            ).messages.create(**params)
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        usage = usage_from_sdk(message.usage)
        return ProviderResponse(
            text=text,
            usage=usage,
            stop_reason=str(message.stop_reason),
            request_id=getattr(message, "_request_id", None),
        )

    async def converse(self, request: ConverseRequest) -> ConverseResponse:
        """One turn with client tools: ``tools`` in, ``tool_use`` blocks out. The caller
        executes tools and sends ``tool_result`` blocks back in the next user message."""
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": list(request.messages),
        }
        if request.system:
            params["system"] = request.system
        if request.tools:
            params["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    **({"strict": True} if t.strict else {}),
                }
                for t in request.tools
            ]
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        message = await _call(
            self._client.with_options(
                timeout=request.timeout_seconds, max_retries=0
            ).messages.create(**params)
        )
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        content: list[dict[str, Any]] = []
        for block in message.content:
            if block.type == "text":
                text_parts.append(block.text)
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, input=dict(args)))
                content.append(
                    {"type": "tool_use", "id": block.id, "name": block.name, "input": args}
                )
            # Other block types (thinking, server tools) are not requested by this app.
        return ConverseResponse(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            content=tuple(content),
            stop_reason=str(message.stop_reason),
            usage=usage_from_sdk(message.usage),
            request_id=getattr(message, "_request_id", None),
        )


async def _call(pending: Awaitable[Any]) -> Any:
    """Await an SDK call, mapping SDK exceptions onto our error taxonomy."""
    try:
        return await pending
    except anthropic.RateLimitError as exc:
        raise RateLimited(
            "Anthropic rate limit", retry_after=_retry_after(exc), status=429
        ) from None
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise ProviderAuthError("Anthropic credentials rejected", status=exc.status_code) from None
    except (
        anthropic.BadRequestError,
        anthropic.NotFoundError,
        anthropic.UnprocessableEntityError,
        anthropic.RequestTooLargeError,
    ) as exc:
        raise InvalidRequest("Anthropic rejected the request", status=exc.status_code) from None
    except anthropic.APIStatusError as exc:  # 5xx incl. 529 overloaded, 408, 409
        raise ProviderUnavailable("Anthropic API error", status=exc.status_code) from None
    except anthropic.APITimeoutError:  # subclass of APIConnectionError: catch first
        raise ProviderUnavailable("Anthropic request timed out", status=None) from None
    except anthropic.APIConnectionError:
        raise ProviderUnavailable("Could not reach Anthropic API", status=None) from None


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    raw = exc.response.headers.get("retry-after") if exc.response is not None else None
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def usage_from_sdk(u: Any) -> Usage:
    """Map SDK usage to ours, splitting cache writes by TTL when the breakdown is reported
    (``cache_creation.ephemeral_5m/1h_input_tokens``); otherwise all writes count as 5m."""
    total_writes = u.cache_creation_input_tokens or 0
    breakdown = getattr(u, "cache_creation", None)
    write_1h = (getattr(breakdown, "ephemeral_1h_input_tokens", 0) or 0) if breakdown else 0
    return Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=u.cache_read_input_tokens or 0,
        cache_write_tokens=max(total_writes - write_1h, 0),
        cache_write_1h_tokens=write_1h,
    )
