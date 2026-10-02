"""Request guard: body-size limit and NUL rejection, before anything parses the body.

- Size is enforced on ``Content-Length`` *and* while streaming (chunked bodies can't lie
  their way past it), so an oversized upload is refused without being buffered.
- JSON bodies containing NUL in any string are refused with 422: PostgreSQL can't store it,
  and accepting it would turn a bad request into a server error later.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from solutionforge.core.text import has_nul


class RequestGuardMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        declared = headers.get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await _reject(
                scope,
                send,
                413,
                "payload_too_large",
                f"request body exceeds {self.max_bytes} bytes",
            )
            return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.max_bytes:
                await _reject(
                    scope,
                    send,
                    413,
                    "payload_too_large",
                    f"request body exceeds {self.max_bytes} bytes",
                )
                return

        content_type = headers.get(b"content-type", b"").split(b";")[0].strip().lower()
        if content_type.endswith(b"json") and _json_has_nul(bytes(body)):
            await _reject(
                scope,
                send,
                422,
                "invalid_characters",
                "request contains NUL characters, which are not allowed",
            )
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()  # after the body: disconnect notifications

        await self.app(scope, replay, send)


def _json_has_nul(body: bytes) -> bool:
    if b"\x00" not in body and b"\\u0000" not in body.lower():
        return False  # fast path: the overwhelming majority of requests
    try:
        parsed: Any = json.loads(body)
    except ValueError:
        return False  # malformed JSON: let the framework produce its normal 422
    return has_nul(parsed)


async def _reject(scope: Scope, send: Send, status: int, code: str, message: str) -> None:
    request_id = scope.get("state", {}).get("request_id")
    payload = json.dumps(
        {"error": {"code": code, "message": message, "details": {}, "request_id": request_id}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
