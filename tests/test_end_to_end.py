"""End-to-end: JSON-RPC over HTTP, down through every layer, to a mocked PSE Edge.

Other suites test layers in isolation — repositories against fakes, the freeze policy
against an injected clock, parsers against fixtures. This one exercises the whole vertical
in one request: MCP framing → tool → repository → FreezeService → client → parser → model →
the response envelope a client actually reads.

That matters most for the failure modes. A user does not experience `EdgeUnavailableError`;
they experience whatever JSON comes back from `tools/call`, and nothing else here proved
what that is.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import httpx
import pytest
import respx

from pse_edge_mcp.asgi import transport_security_for
from pse_edge_mcp.config import Settings
from pse_edge_mcp.market_calendar import MarketCalendar
from pse_edge_mcp.server import build_server

BASE = "https://edge.pse.com.ph"
MNL = ZoneInfo("Asia/Manila")
CLOSED = datetime(2026, 7, 30, 16, 30, tzinfo=MNL)  # Thursday, after the 15:00 close
OPEN = datetime(2026, 7, 30, 11, 0, tzinfo=MNL)  # Thursday, mid-session
FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC = "http://localhost:8000"

AUTOCOMPLETE = [
    {"cmpyId": "599", "cmpyNm": "SM Investments Corporation", "symbol": "SM", "etfYn": "0"}
]


class FrozenCalendar(MarketCalendar):
    """A clock the test can advance, so cache entries can be aged past a boundary."""

    def __init__(self, at: datetime) -> None:
        super().__init__()
        self.at = at

    def now(self) -> datetime:
        return self.at


@asynccontextmanager
async def serving(at: datetime = CLOSED, calendar: FrozenCalendar | None = None):
    """The real MCP HTTP app, with only the clock pinned."""
    mcp = build_server(
        Settings(throttle_rate_per_sec=1000, retry_attempts=1, request_timeout_sec=2.0),
        calendar=calendar or FrozenCalendar(at),
    )
    app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=transport_security_for(PUBLIC),
    )
    started = anyio.Event()
    done = anyio.Event()

    async def lifespan() -> None:
        async def receive():
            if not started.is_set():
                return {"type": "lifespan.startup"}
            await done.wait()
            return {"type": "lifespan.shutdown"}

        async def send(message):
            if message["type"] == "lifespan.startup.complete":
                started.set()

        await app({"type": "lifespan"}, receive, send)

    async with anyio.create_task_group() as tg:
        tg.start_soon(lifespan)
        await started.wait()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=PUBLIC
            ) as http:
                yield http
        finally:
            done.set()


async def rpc(http: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    response = await http.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def call_tool(http: httpx.AsyncClient, name: str, **arguments) -> dict:
    body = await rpc(http, "tools/call", {"name": name, "arguments": arguments})
    result = body["result"]
    payload = result.get("structuredContent")
    if payload is None:
        text = result["content"][0]["text"]
        payload = json.loads(text) if isinstance(text, str) else text
    return payload


def mock_edge_ok() -> None:
    respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=AUTOCOMPLETE)
    )


# --- the happy path, whole stack ---------------------------------------------


@respx.mock
async def test_the_full_protocol_journey_a_client_actually_performs():
    mock_edge_ok()
    async with serving() as http:
        init = await rpc(
            http,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "1"},
            },
        )
        assert init["result"]["serverInfo"]["name"] == "pse-edge"
        assert init["result"]["serverInfo"]["version"], "serverInfo.version must not be empty"

        listed = await rpc(http, "tools/list")
        names = {t["name"] for t in listed["result"]["tools"]}
        assert {"search_companies", "validate_symbol", "get_stock_quote"} <= names
        assert "send_email" not in names, "the action tool needs auth, which is off here"

        payload = await call_tool(http, "validate_symbol", symbol="sm")

    assert payload["data"] == {
        "valid": True,
        "symbol": "SM",
        "company_id": "599",
        "company_name": "SM Investments Corporation",
    }
    meta = payload["meta"]
    assert meta["data_policy"] == "EOD-frozen"
    assert meta["stale"] is False and meta["from_cache"] is False
    assert meta["as_of"] and meta["valid_until"], "freshness must travel with every result"


# --- what a user sees when PSE Edge misbehaves -------------------------------


@respx.mock
async def test_an_unreachable_edge_with_nothing_cached_returns_edge_unavailable():
    """A user never sees the exception; they see whatever tools/call returns."""
    respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    async with serving() as http:
        payload = await call_tool(http, "search_companies", query="sm")

    assert payload["error"] == "EDGE_UNAVAILABLE"
    assert "unreachable" in payload["message"].lower()
    assert "data" not in payload


@respx.mock
async def test_an_outage_serves_the_last_close_flagged_stale_through_the_whole_stack():
    """The behaviour that matters during a real PSE Edge outage: a warm cache keeps the
    server answering, honestly labelled, instead of failing.

    The clock is advanced past the next close first, so the entry is genuinely EXPIRED —
    otherwise this is an ordinary cache hit and proves nothing about the outage path.
    """
    route = respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=AUTOCOMPLETE)
    )
    clock = FrozenCalendar(CLOSED)

    async with serving(calendar=clock) as http:
        first = await call_tool(http, "validate_symbol", symbol="SM")
        assert first["meta"]["from_cache"] is False
        assert first["meta"]["stale"] is False

        # A day passes — the entry is now past its boundary — and PSE Edge goes down.
        clock.at = CLOSED + timedelta(days=1)
        route.mock(side_effect=httpx.ConnectError("connection refused"))
        outage = await call_tool(http, "validate_symbol", symbol="SM")

    assert "error" not in outage, "a warm cache must survive an upstream outage"
    assert outage["data"]["company_id"] == "599", "the last close still answers"
    assert outage["meta"]["stale"] is True, "and is honestly labelled as past its boundary"
    assert outage["meta"]["from_cache"] is True
    assert outage["meta"]["as_of"] == first["meta"]["as_of"], "as_of says how old it is"


@respx.mock
async def test_market_hours_refuse_an_uncached_read_with_a_retry_timestamp():
    """The freeze policy as an agent experiences it: a structured refusal it can schedule
    against, not a failure it should retry immediately."""
    mock_edge_ok()
    async with serving(at=OPEN) as http:
        payload = await call_tool(http, "search_companies", query="sm")

    assert payload["error"] == "MARKET_OPEN_NO_CACHE"
    assert payload["retry_after"], "an agent needs to know when to come back"
    assert not respx.calls, "invariant #1: not one upstream request during a session"


@respx.mock
async def test_a_restyled_page_surfaces_as_endpoint_changed_not_partial_data():
    """Invariant #4 from the caller's side: loud, structured, never a half-filled model."""
    respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=AUTOCOMPLETE)
    )
    respx.get(f"{BASE}/companyPage/stockData.do").mock(
        return_value=httpx.Response(200, text="<html><body>redesigned</body></html>")
    )
    async with serving() as http:
        payload = await call_tool(http, "get_stock_quote", symbol="SM")

    assert payload["error"] in {"ENDPOINT_CHANGED", "INTERNAL_ERROR"}
    assert "data" not in payload


@respx.mock
async def test_an_invalid_argument_is_a_structured_error_not_a_crash():
    mock_edge_ok()
    async with serving() as http:
        payload = await call_tool(http, "validate_symbol", symbol="   ")

    assert payload["error"] == "INVALID_ARGUMENT"


# --- the canary CLI ----------------------------------------------------------


@respx.mock
def test_the_canary_cli_exits_zero_when_every_family_is_healthy(monkeypatch):
    """Exit status is what lets cron or CI notice without parsing output."""
    from pse_edge_mcp import canary

    monkeypatch.setattr(
        canary, "run_and_notify", _fake_report(ok=True), raising=True
    )
    with pytest.raises(SystemExit) as exit_info:
        canary.main()
    assert exit_info.value.code == 0


@respx.mock
def test_the_canary_cli_exits_non_zero_when_a_family_broke(monkeypatch):
    from pse_edge_mcp import canary

    monkeypatch.setattr(canary, "run_and_notify", _fake_report(ok=False), raising=True)
    with pytest.raises(SystemExit) as exit_info:
        canary.main()
    assert exit_info.value.code == 1


def _fake_report(*, ok: bool) -> Any:
    from pse_edge_mcp.canary import CanaryReport, CheckResult

    async def run(_settings=None, **_kwargs):
        report = CanaryReport()
        report.checks.append(
            CheckResult("get_indices", ok, "" if ok else "ValidationError: rows missing")
        )
        return report

    return run
