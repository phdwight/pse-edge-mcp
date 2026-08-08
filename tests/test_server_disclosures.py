"""Tool-layer tests for the disclosure tools.

These exercise the routing decisions the tools make — which Edge endpoint serves a
given query, what gets cached under which key, and how errors surface — with the
freeze clock pinned to a closed-market moment and all HTTP mocked.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import respx

from pse_edge_mcp.config import Settings
from pse_edge_mcp.market_calendar import MarketCalendar
from pse_edge_mcp.server import build_server

BASE = "https://edge.pse.com.ph"
MNL = ZoneInfo("Asia/Manila")
CLOSED = datetime(2026, 7, 30, 16, 30, tzinfo=MNL)  # Thursday, post-close


class FrozenCalendar(MarketCalendar):
    """Pin 'now' so tests never depend on when they run."""

    def now(self) -> datetime:
        return CLOSED


def build_test_server():
    settings = Settings(throttle_rate_per_sec=1000, retry_attempts=2)
    return build_server(settings, calendar=FrozenCalendar())


async def call(mcp, name: str, **arguments) -> dict:
    result = await mcp.call_tool(name, arguments)
    payload = result.content[0].text  # type: ignore[union-attr]
    return json.loads(payload) if isinstance(payload, str) else payload


AUTOCOMPLETE_SM = [
    {"cmpyId": "599", "cmpyNm": "SM Investments Corporation", "symbol": "SM", "etfYn": "0"}
]


def mock_autocomplete():
    return respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=AUTOCOMPLETE_SM)
    )


@respx.mock
async def test_symbol_without_dates_uses_full_history_endpoint(company_disclosures_html):
    """A bare symbol wants everything the company ever filed — companyDisclosures has
    no date filter and serves exactly that; announcements would need a window."""
    mock_autocomplete()
    company_route = respx.post(f"{BASE}/companyDisclosures/search.ax").mock(
        return_value=httpx.Response(200, text=company_disclosures_html)
    )
    announce_route = respx.post(f"{BASE}/announcements/search.ax")

    result = await call(build_test_server(), "search_disclosures", symbol="SM")

    assert company_route.called
    assert not announce_route.called
    assert result["data"]["source"] == "company_disclosures"
    assert result["data"]["total"] == 343
    assert result["data"]["has_more"] is True
    assert len(result["data"]["hits"]) == 50
    assert result["meta"]["data_policy"] == "daily-refresh"


@respx.mock
async def test_date_range_uses_announcements_and_passes_company_filter(announcements_html):
    mock_autocomplete()
    route = respx.post(f"{BASE}/announcements/search.ax").mock(
        return_value=httpx.Response(200, text=announcements_html)
    )

    result = await call(
        build_test_server(),
        "search_disclosures",
        symbol="SM",
        start_date="2026-07-01",
        end_date="2026-07-30",
    )

    assert result["data"]["source"] == "announcements"
    form = dict(pair.split("=", 1) for pair in route.calls.last.request.content.decode().split("&"))
    assert form["companyId"] == "599"
    assert form["fromDate"] == "07-01-2026" and form["toDate"] == "07-30-2026"


@respx.mock
async def test_market_wide_search_needs_no_symbol(announcements_html):
    auto = mock_autocomplete()
    respx.post(f"{BASE}/announcements/search.ax").mock(
        return_value=httpx.Response(200, text=announcements_html)
    )

    result = await call(
        build_test_server(), "search_disclosures", start_date="2026-07-01", end_date="2026-07-30"
    )

    assert not auto.called  # no symbol to resolve
    assert result["data"]["hits"][0]["company_name"] == "Lepanto Consolidated Mining Company"


@respx.mock
async def test_repeat_query_is_served_from_cache_without_a_second_upstream_hit(
    announcements_html,
):
    route = respx.post(f"{BASE}/announcements/search.ax").mock(
        return_value=httpx.Response(200, text=announcements_html)
    )
    mcp = build_test_server()
    args = {"start_date": "2026-07-01", "end_date": "2026-07-30"}

    first = await call(mcp, "search_disclosures", **args)
    second = await call(mcp, "search_disclosures", **args)

    assert route.call_count == 1
    assert first["meta"]["from_cache"] is False
    assert second["meta"]["from_cache"] is True


@respx.mock
async def test_different_pages_are_cached_separately(announcements_html):
    route = respx.post(f"{BASE}/announcements/search.ax").mock(
        return_value=httpx.Response(200, text=announcements_html)
    )
    mcp = build_test_server()
    args = {"start_date": "2026-07-01", "end_date": "2026-07-30"}

    await call(mcp, "search_disclosures", page=1, **args)
    await call(mcp, "search_disclosures", page=2, **args)

    assert route.call_count == 2


@respx.mock
async def test_unknown_symbol_returns_structured_error():
    respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await call(build_test_server(), "search_disclosures", symbol="NOSUCH")
    assert result["error"] == "SYMBOL_NOT_FOUND"


@respx.mock
async def test_bad_date_and_bad_page_are_rejected_before_any_upstream_call():
    route = respx.post(f"{BASE}/announcements/search.ax")
    mcp = build_test_server()

    bad_date = await call(mcp, "search_disclosures", start_date="30-07-2026")
    reversed_range = await call(
        mcp, "search_disclosures", start_date="2026-07-30", end_date="2026-07-01"
    )
    bad_page = await call(mcp, "search_disclosures", page=0)

    assert bad_date["error"] == "INVALID_ARGUMENT"
    assert reversed_range["error"] == "INVALID_ARGUMENT"
    assert bad_page["error"] == "INVALID_ARGUMENT"
    assert not route.called


@respx.mock
async def test_get_disclosure_returns_absolute_links_and_immutable_meta(
    disclosure_viewer_html,
):
    route = respx.get(f"{BASE}/openDiscViewer.do").mock(
        return_value=httpx.Response(200, text=disclosure_viewer_html)
    )
    mcp = build_test_server()
    edge_no = "ff4c7557aee1d72b64d70b69f0a3140b"

    result = await call(mcp, "get_disclosure", edge_no=edge_no)
    data = result["data"]

    assert data["company_name"] == "Lepanto Consolidated Mining Company"
    assert data["attachments"][0]["download_url"] == f"{BASE}/downloadFile.do?file_id=1949133"
    assert data["body_html_url"] == f"{BASE}/downloadHtml.do?file_id=1949127"
    assert result["meta"]["data_policy"] == "immutable"
    assert result["meta"]["valid_until"] is None

    # Immutable: a second call must not go upstream again.
    await call(mcp, "get_disclosure", edge_no=edge_no)
    assert route.call_count == 1


@respx.mock
async def test_get_disclosure_rejects_malformed_edge_no_without_calling_edge():
    route = respx.get(f"{BASE}/openDiscViewer.do")
    result = await call(build_test_server(), "get_disclosure", edge_no="not-a-real-id")
    assert result["error"] == "INVALID_ARGUMENT"
    assert not route.called


@respx.mock
async def test_attachment_resource_serves_bytes_and_caches_immutably():
    """The pse-edge://attachment/{file_id} resource is how hosts that cannot fetch URLs
    read a disclosure file: bytes out, one upstream hit ever."""
    pdf = b"%PDF-1.4 resource fixture"
    route = respx.get(f"{BASE}/downloadFile.do").mock(
        return_value=httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
    )
    mcp = build_test_server()

    templates = await mcp.list_resource_templates()
    assert any("pse-edge://attachment/" in str(t.uri_template) for t in templates)

    first = await mcp.read_resource("pse-edge://attachment/1949133")
    [content] = list(first)
    assert content.content == pdf

    again = await mcp.read_resource("pse-edge://attachment/1949133")
    assert list(again)[0].content == pdf
    assert route.call_count == 1, "immutable: the file is fetched from PSE Edge once ever"


@respx.mock
async def test_attachment_resource_rejects_a_non_numeric_file_id_without_calling_edge():
    route = respx.get(f"{BASE}/downloadFile.do")
    mcp = build_test_server()
    import pytest

    # A traversal-shaped URI never matches the template — the SDK refuses it first.
    with pytest.raises(Exception, match="Unknown resource"):
        await mcp.read_resource("pse-edge://attachment/../oauth")
    # A matching-but-non-numeric id reaches our validator, which refuses it (the SDK
    # wraps the InvalidArgumentError in its template-error message).
    with pytest.raises(Exception, match="Error creating resource"):
        await mcp.read_resource("pse-edge://attachment/DROP")
    assert not route.called, "either way, nothing reaches PSE Edge"


@respx.mock
async def test_get_disclosure_rejects_an_out_of_range_max_files_without_calling_edge():
    route = respx.get(f"{BASE}/openDiscViewer.do")
    edge_no = "ff4c7557aee1d72b64d70b69f0a3140b"
    mcp = build_test_server()

    zero = await call(mcp, "get_disclosure", edge_no=edge_no, max_files=0)
    huge = await call(mcp, "get_disclosure", edge_no=edge_no, max_files=1000)

    assert zero["error"] == "INVALID_ARGUMENT"
    assert huge["error"] == "INVALID_ARGUMENT"
    assert not route.called


@respx.mock
async def test_fulltext_tool_reports_coverage_limits(keyword_search_html):
    respx.post(f"{BASE}/keyword/search.ax").mock(
        return_value=httpx.Response(200, text=keyword_search_html)
    )
    result = await call(build_test_server(), "search_disclosure_fulltext", keyword="dividend")

    assert result["data"]["total"] == 10666
    assert len(result["data"]["hits"]) == 10
    assert "2023-2025" in result["data"]["coverage_note"]
    assert result["data"]["hits"][0]["snippet"]


@respx.mock
async def test_endpoint_drift_surfaces_as_endpoint_changed():
    respx.post(f"{BASE}/announcements/search.ax").mock(
        return_value=httpx.Response(200, text="<html><body>redesigned</body></html>")
    )
    result = await call(
        build_test_server(), "search_disclosures", start_date="2026-07-01", end_date="2026-07-30"
    )
    assert result["error"] == "ENDPOINT_CHANGED"


async def test_tool_surface_is_stable():
    """Guards the MCP contract against internal refactors.

    The SDK derives each tool's schema from its signature and docstring, so a change in
    how tools are wired can silently alter the public surface — a wrapper that hides
    parameters, or a lost docstring, degrades what the client sees without failing any
    behavioural test.
    """
    tools = await build_test_server().list_tools()
    surface = {t.name: sorted((t.input_schema or {}).get("required", [])) for t in tools}
    assert surface == {
        "search_companies": ["query"],
        "validate_symbol": ["symbol"],
        "get_stock_quote": ["symbol"],
        "get_price_history": ["symbol"],
        "search_disclosures": [],
        "search_disclosure_fulltext": ["keyword"],
        "get_disclosure": ["edge_no"],
        "get_company_profile": ["symbol"],
        "get_financial_highlights": ["symbol"],
        "get_dividends_and_rights": ["symbol"],
        "get_indices": [],
        "get_market_summary": [],
        "get_server_version": [],
    }
    # Descriptions come from the docstrings and are how the model picks a tool.
    assert all(t.description for t in tools)
    assert "EOD-frozen" in dict((t.name, t.description) for t in tools)["get_stock_quote"]
    # Every data tool declares itself a pure read so hosts can auto-approve it.
    assert all(
        t.annotations is not None and t.annotations.read_only_hint is True for t in tools
    ), "a data tool without readOnlyHint costs the user a permission prompt per call"
    # And every tool carries a human-friendly title for host catalogs.
    assert all(t.title for t in tools)


async def test_prompts_are_listed_and_render_with_the_symbol():
    """The two canned prompts hosts surface in their pickers: one argumentless recap,
    one per-company briefing whose symbol is normalised into the rendered text."""
    mcp = build_test_server()

    prompts = {p.name for p in await mcp.list_prompts()}
    assert {"market_recap", "company_briefing"} <= prompts

    rendered = await mcp.get_prompt("company_briefing", {"symbol": "sm"})
    text = rendered.messages[0].content.text  # type: ignore[union-attr]
    assert " SM." in text and "validate_symbol" in text
    assert "meta.note" in text, "the prompt must carry the not-realtime relay rule"

    recap = await mcp.get_prompt("market_recap", None)
    assert "get_indices" in recap.messages[0].content.text  # type: ignore[union-attr]


async def test_get_server_version_reports_the_installed_release():
    """The deployed version, from the installed distribution — the same value
    serverInfo and /health carry, so the three can never disagree."""
    import importlib.metadata

    result = await call(build_test_server(), "get_server_version")

    assert result["data"]["name"] == "pse-edge"
    assert result["data"]["version"] == importlib.metadata.version("pse-edge-mcp")
    assert "meta" not in result, "a version has no freshness contract to report"


# --- validate_symbol ---------------------------------------------------------


AUTOCOMPLETE_AREIT = [
    {"cmpyId": "700", "cmpyNm": "AREIT, Inc.", "symbol": "AREIT", "etfYn": "0"},
    {"cmpyId": "701", "cmpyNm": "Areit Holdings Corp.", "symbol": "AREITH", "etfYn": "0"},
]


@respx.mock
async def test_validate_symbol_confirms_a_known_ticker():
    respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=AUTOCOMPLETE_AREIT)
    )
    result = await call(build_test_server(), "validate_symbol", symbol="AREIT")

    assert result["data"] == {
        "valid": True,
        "symbol": "AREIT",
        "company_name": "AREIT, Inc.",
        "company_id": "700",
    }
    # The freshness contract applies here like everywhere else.
    assert result["meta"]["data_policy"] == "daily-refresh"
    assert result["meta"]["as_of"] and result["meta"]["valid_until"]


@respx.mock
async def test_validate_symbol_is_case_insensitive_and_normalises_the_echo():
    respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=AUTOCOMPLETE_AREIT)
    )
    result = await call(build_test_server(), "validate_symbol", symbol="  areit ")

    assert result["data"]["valid"] is True
    assert result["data"]["symbol"] == "AREIT", "echo the normalised form, not the input"
    assert result["data"]["company_id"] == "700"


@respx.mock
async def test_validate_symbol_answers_false_rather_than_erroring():
    """An agent checking a symbol is asking a question; "no" is a good answer to it."""
    respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await call(build_test_server(), "validate_symbol", symbol="NOTATICKER")

    assert result["data"] == {
        "valid": False,
        "symbol": "NOTATICKER",
        "company_name": None,
        "company_id": None,
    }
    assert "error" not in result


@respx.mock
async def test_validate_symbol_does_not_accept_a_prefix():
    """Autocomplete is a prefix search, so AREIT would match a query for ARE. Treating
    that as valid would greenlight a symbol that no other tool can then resolve."""
    respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=AUTOCOMPLETE_AREIT)
    )
    result = await call(build_test_server(), "validate_symbol", symbol="ARE")

    assert result["data"]["valid"] is False
    assert result["data"]["company_id"] is None
