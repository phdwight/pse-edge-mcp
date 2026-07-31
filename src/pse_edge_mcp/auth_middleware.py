"""ASGI middleware enforcing bearer auth and quotas ahead of the MCP app.

Pure ASGI rather than a Starlette `BaseHTTPMiddleware` on purpose: it adds no
dependency, it cannot interfere with response streaming, and the contract is small
enough to state completely — reject before the app ever sees the request, or step
aside entirely.

Refusals are HTTP-level, not MCP tool errors, because they happen before MCP dispatch:

- 401 with `WWW-Authenticate: Bearer` when credentials are missing or invalid (the
  header is what the MCP authorization spec tells clients to look for; stage 2 adds the
  `resource_metadata` pointer once the RFC 9728 endpoint exists).
- 429 with `Retry-After` and a structured `RATE_LIMITED` body when over quota — the
  same code the tool layer uses, so clients handle one vocabulary.

`/.well-known/*` is exempt so OAuth discovery (stage 2) can never lock itself out.
"""

from __future__ import annotations

import json
from typing import Any

from .auth import QuotaTracker, TokenService

_EXEMPT_PREFIXES = ("/.well-known/",)


class AuthMiddleware:
    def __init__(self, app: Any, tokens: TokenService, quotas: QuotaTracker) -> None:
        self._app = app
        self._tokens = tokens
        self._quotas = quotas

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_EXEMPT_PREFIXES):
            await self._app(scope, receive, send)
            return

        token = _bearer_token(scope)
        if token is None:
            await _refuse(
                send,
                401,
                {"error": "UNAUTHORIZED", "message": "Missing bearer token."},
                [(b"www-authenticate", b"Bearer")],
            )
            return

        context = await self._tokens.authenticate(token)
        if context is None:
            await _refuse(
                send,
                401,
                {"error": "UNAUTHORIZED", "message": "Invalid, expired or revoked token."},
                [(b"www-authenticate", b'Bearer error="invalid_token"')],
            )
            return

        decision = self._quotas.check(context)
        if not decision.allowed:
            await _refuse(
                send,
                429,
                {
                    "error": "RATE_LIMITED",
                    "message": "Request quota exceeded; retry after the window resets.",
                    "retry_after_seconds": decision.retry_after_seconds,
                },
                [(b"retry-after", str(decision.retry_after_seconds).encode())],
            )
            return

        scope["pse_auth"] = context  # available to anything downstream that cares
        await self._app(scope, receive, send)


def _bearer_token(scope: dict[str, Any]) -> str | None:
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for name, value in headers:
        if name == b"authorization":
            scheme, _, credential = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and credential.strip():
                return credential.strip()
            return None
    return None


async def _refuse(
    send: Any, status: int, body: dict[str, Any], extra_headers: list[tuple[bytes, bytes]]
) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *extra_headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
