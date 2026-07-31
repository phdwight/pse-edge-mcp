"""Liveness and readiness endpoints for load balancers and orchestrators.

Two endpoints, because they answer different questions and a deployment that conflates
them behaves badly:

- `GET /health` — **liveness**. Is this process alive and serving? Cheap, no dependencies,
  always 200 if the event loop is running. A liveness probe that touches the database will
  restart every replica during a database blip, turning a recoverable outage into an outage
  plus a thundering herd of restarts.
- `GET /health/ready` — **readiness**. Can this replica serve real traffic *right now*?
  Checks the database when one is configured. A failing readiness probe should take the
  replica out of rotation, not kill it.

Both sit outside authentication: a probe cannot hold a token, and an orchestrator that
cannot health-check the service will simply never route to it. They are also outside the
MCP path entirely, so a probe never touches PSE Edge or the freeze cache.
"""

from __future__ import annotations

import json
import time
from typing import Any

LIVENESS_PATH = "/health"
READINESS_PATH = "/health/ready"

_STARTED_AT = time.monotonic()


class HealthApp:
    """Answers the probe paths; passes everything else to the wrapped app."""

    def __init__(self, app: Any, *, engine: Any = None, version: str = "") -> None:
        self._app = app
        self._engine = engine
        self._version = version

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] not in (LIVENESS_PATH, READINESS_PATH):
            await self._app(scope, receive, send)
            return

        if scope["path"] == LIVENESS_PATH:
            await _respond(
                send,
                200,
                {
                    "status": "ok",
                    "version": self._version,
                    "uptime_seconds": round(time.monotonic() - _STARTED_AT, 1),
                },
            )
            return

        await _respond(send, *await self._readiness())

    async def _readiness(self) -> tuple[int, dict[str, Any]]:
        if self._engine is None:
            # No database configured is a valid deployment (in-memory cache), not a fault.
            return 200, {"status": "ready", "database": "not configured"}
        try:
            from sqlalchemy import text  # noqa: PLC0415

            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            # 503, never 500: this is "do not route to me yet", a normal state during
            # startup or a database failover, not a bug in this process.
            return 503, {"status": "not ready", "database": "unreachable", "error": str(exc)[:200]}
        return 200, {"status": "ready", "database": "ok"}


async def _respond(send: Any, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # Probes must never be served from a cache, or a dead replica keeps
                # looking healthy.
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
