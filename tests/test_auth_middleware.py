"""The auth middleware's HTTP contract, driven through a real ASGI client.

A trivial inner app stands in for the MCP server: the middleware's job is to refuse
before the app ever sees the request, or step aside entirely, so these tests assert on
exactly that boundary — status codes, headers, and whether the inner app ran.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from pse_edge_mcp.auth import QuotaTracker, TokenRecord, TokenService, generate_token, hash_token
from pse_edge_mcp.auth_middleware import AuthMiddleware

T0 = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


class InnerApp:
    """Minimal ASGI app recording what reached it."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.requests.append({"path": scope["path"], "auth": scope.get("pse_auth")})
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok": true}'})


class FakeStore:
    def __init__(self) -> None:
        self.records: dict[str, TokenRecord] = {}

    async def lookup(self, token_hash: str) -> TokenRecord | None:
        return self.records.get(token_hash)


def build(quota_per_minute: int = 100) -> tuple[Any, InnerApp, str]:
    """A wrapped app, its inner recorder, and one valid bearer token."""
    inner = InnerApp()
    store = FakeStore()
    token = generate_token()
    store.records[hash_token(token)] = TokenRecord(
        user_id="u1",
        email="user@example.com",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        user_disabled_at=None,
        quota_per_minute=quota_per_minute,
        quota_per_day=10_000,
    )
    wrapped = AuthMiddleware(inner, TokenService(store), QuotaTracker())
    return wrapped, inner, token


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_missing_token_is_401_with_www_authenticate_and_never_reaches_the_app():
    app, inner, _ = build()
    async with client(app) as http:
        response = await http.post("/mcp", json={})

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.json()["error"] == "UNAUTHORIZED"
    assert inner.requests == [], "the app must not see unauthenticated requests"


async def test_wrong_scheme_and_unknown_token_are_401():
    app, inner, _ = build()
    async with client(app) as http:
        basic = await http.post("/mcp", headers={"Authorization": "Basic dXNlcjpwdw=="})
        unknown = await http.post("/mcp", headers={"Authorization": f"Bearer {generate_token()}"})

    assert basic.status_code == 401
    assert unknown.status_code == 401
    assert unknown.json()["message"].startswith("Invalid")
    assert inner.requests == []


async def test_valid_token_passes_through_with_auth_context_attached():
    app, inner, token = build()
    async with client(app) as http:
        response = await http.post("/mcp", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(inner.requests) == 1
    assert inner.requests[0]["auth"].email == "user@example.com"


async def test_over_quota_is_429_with_retry_after():
    app, inner, token = build(quota_per_minute=2)
    async with client(app) as http:
        headers = {"Authorization": f"Bearer {token}"}
        assert (await http.post("/mcp", headers=headers)).status_code == 200
        assert (await http.post("/mcp", headers=headers)).status_code == 200
        denied = await http.post("/mcp", headers=headers)

    assert denied.status_code == 429
    assert denied.json()["error"] == "RATE_LIMITED"
    assert 0 < int(denied.headers["retry-after"]) <= 60
    assert denied.json()["retry_after_seconds"] == int(denied.headers["retry-after"])
    assert len(inner.requests) == 2, "the denied request must not reach the app"


async def test_well_known_paths_bypass_auth_for_oauth_discovery():
    """Stage 2's RFC 9728 metadata endpoint must never be able to lock itself out."""
    app, inner, _ = build()
    async with client(app) as http:
        response = await http.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    assert inner.requests[0]["path"] == "/.well-known/oauth-protected-resource"
    assert inner.requests[0]["auth"] is None
