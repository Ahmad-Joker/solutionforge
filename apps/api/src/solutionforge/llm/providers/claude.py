"""Anthropic (Claude) adapter using the official ``anthropic`` SDK.

This is the only module that imports the SDK. SDK retries are disabled
(``max_retries=0``): :class:`~solutionforge.llm.service.LLMService` owns retry, fallback and
metering, so every attempt is visible and billed exactly once in our ledger.
"""

from __future__ import annotations

from typing import Any

import anthropic

from solutionforge.llm.types import (
    InvalidRequest,
    ProviderAuthError,
    ProviderRequest,
    ProviderResponse,
    ProviderUnavailable,
    RateLimited,
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

        try:
            message = await self._client.with_options(
                timeout=request.timeout_seconds, max_retries=0
            ).messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise RateLimited(
                "Anthropic rate limit", retry_after=_retry_after(exc), status=429
            ) from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderAuthError(
                "Anthropic credentials rejected", status=exc.status_code
            ) from None
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

        text = "".join(block.text for block in message.content if block.type == "text")
        u = message.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        return ProviderResponse(
            text=text,
            usage=usage,
            stop_reason=str(message.stop_reason),
            request_id=getattr(message, "_request_id", None),
        )


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    raw = exc.response.headers.get("retry-after") if exc.response is not None else None
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None
