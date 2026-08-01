"""Consuming this MCP server from a multi-tenant app (LangGraph or otherwise).

The shape this assumes, and why:

    your users → your app's own login → your agent → [ONE machine client] → this server

Your app authenticates as **itself**, not as each of your users. That is the right split
when you own both sides: your users never learn this server exists, never sign up twice,
and never enroll a second passkey. Per-user identity stays entirely in your app, where it
already is. (The alternative — per-user OAuth delegation — only pays off when the MCP
server is a third party with its own user relationship.)

Provision the credential once from `/account` on the MCP server (a **Machine clients**
panel appears for accounts listed in `PSE_ADMIN_EMAILS`), then raise its quota, since all
your users now share one budget:

    pse-edge-admin set-quota <shown-service-account> --per-minute 600 --per-day 50000

Keep `client_id`/`client_secret` server-side. They are not user credentials and must never
reach a browser.

INSTALL NOTE: `langchain-mcp-adapters` currently requires **mcp < 2** — the 2.x SDK moved
`RequestContext` and the adapter fails to import against it. Pin `mcp<2` in the app that
runs this file. This MCP *server* is unaffected; only the client library is.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator

import httpx

TOKEN_ENDPOINT = f"{os.environ.get('PSE_MCP_BASE', 'https://pse.sakayandgo.com')}/oauth/token"
MCP_ENDPOINT = f"{os.environ.get('PSE_MCP_BASE', 'https://pse.sakayandgo.com')}/mcp"


class PseEdgeAuth(httpx.Auth):
    """Attaches a bearer token, minting and re-minting it as needed.

    An `httpx.Auth` rather than a static `headers` dict because the grant issues a
    **1-hour token and no refresh token** — a header captured at startup goes stale
    mid-session and every later call 401s. This mints lazily, reuses the token until it is
    nearly expired, and retries once on a 401 (covering the case where the token was
    revoked, or expired between the check and the call).

    One lock, so a burst of concurrent requests on a cold cache mints once rather than
    once per request — which would otherwise trip the token endpoint's own rate limit.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        token_endpoint: str = TOKEN_ENDPOINT,
        resource: str = MCP_ENDPOINT,
        leeway_seconds: int = 60,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_endpoint = token_endpoint
        self._resource = resource
        self._leeway = leeway_seconds
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _mint(self) -> str:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                self._token_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "mcp",
                    "resource": self._resource,
                },
            )
        if response.status_code != 200:
            # Fail loudly with the server's own error code: `invalid_client` means the
            # credential is wrong or revoked, `unauthorized_client` means it is not a
            # machine client. Both are configuration faults, not transient.
            raise RuntimeError(f"token mint failed ({response.status_code}): {response.text[:200]}")
        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = time.monotonic() + payload.get("expires_in", 3600)
        return self._token

    async def _current(self, *, force: bool = False) -> str:
        async with self._lock:
            fresh = self._token and time.monotonic() < self._expires_at - self._leeway
            if force or not fresh:
                return await self._mint()
            assert self._token is not None
            return self._token

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._current()}"
        response = yield request
        if response.status_code == 401:
            # Revoked, or expired inside the leeway window. One retry with a fresh token;
            # a second 401 is a real failure and is allowed to surface.
            request.headers["Authorization"] = f"Bearer {await self._current(force=True)}"
            yield request


def pse_edge_connection() -> dict[str, object]:
    """The `MultiServerMCPClient` entry for this server."""
    return {
        "transport": "streamable_http",
        "url": MCP_ENDPOINT,
        "auth": PseEdgeAuth(
            os.environ["PSE_CLIENT_ID"],
            os.environ["PSE_CLIENT_SECRET"],
        ),
        "timeout": 30.0,
    }


async def load_tools() -> list:
    """LangChain tools for the graph. Bind these to your model / ToolNode as usual."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({"pse-edge": pse_edge_connection()})
    return await client.get_tools()


# --- what to tell the agent ---------------------------------------------------
#
# Paste into your system prompt. These are the rules that are NOT discoverable from the
# tool schemas, and each one prevents a specific avoidable failure.

AGENT_INSTRUCTIONS = """\
You have PSE Edge tools for Philippine Stock Exchange data.

This data is END-OF-DAY by design. Every successful result carries `meta` (`as_of`,
`valid_until`, `stale`) — read it, and never state a price without saying how fresh it is.
`meta.stale: true` means the market is open and you are seeing the last close; say so
rather than implying it is live.

Tool results may return `{"error": CODE}` instead of data. React, do not retry blindly:
- MARKET_OPEN_NO_CACHE — the market is open and this was not cached. Do NOT retry now; the
  payload carries `retry_after`. Tell the user the figure is unavailable intraday.
- EDGE_UNAVAILABLE — the upstream source is unreachable and nothing was cached. Retry later.
- SYMBOL_NOT_FOUND — use search_companies to find the right ticker.
- INVALID_ARGUMENT — fix the arguments. Dates are YYYY-MM-DD.
- RATE_LIMITED — honour `retry_after_seconds`.

Choosing tools:
- validate_symbol(symbol) — cheap yes/no check that a ticker exists. Returns valid:false
  for unknowns, which is an answer, not an error.
- search_companies(query) — fuzzy search when you do NOT know the exact symbol.
- get_stock_quote / get_price_history — for a symbol you already know is valid.
- search_disclosure_fulltext covers only ~2023-2025, so it is NOT a substitute for
  search_disclosures.
- get_financial_highlights returns figures exactly as published and NEVER rescaled; each
  period reports its own `currency_units`, which can differ between annual and quarterly.
  Read the units before comparing numbers.
- The PSE publishes no gainers/losers/most-active data. It does not exist upstream — say
  so rather than implying it is missing.

Do not call send_email. It emails the credential owner, not the person you are talking to.
"""
