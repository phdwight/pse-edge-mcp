"""Disclosure tests: parsers against recorded fixtures + client wire dialects.

All HTTP is mocked (respx). Fixtures were captured live on 2026-07-30 during closed
hours; see docs/endpoints.md §3.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from pse_edge_mcp.client import PseEdgeClient
from pse_edge_mcp.config import Settings
from pse_edge_mcp.errors import EndpointChangedError
from pse_edge_mcp.parsers import (
    parse_disclosure_table,
    parse_disclosure_viewer,
    parse_keyword_results,
)

BASE = "https://edge.pse.com.ph"
MANILA = ZoneInfo("Asia/Manila")


def make_client() -> PseEdgeClient:
    return PseEdgeClient(Settings(throttle_rate_per_sec=1000, retry_attempts=2))


# --- announcements table (market-wide dialect) --------------------------------


def test_announcements_page_parses_50_rows_with_exact_pagination(announcements_html):
    result = parse_disclosure_table(announcements_html)

    assert len(result["rows"]) == 50
    assert result["total"] == 804
    assert result["page"] == 1
    assert result["pages"] == 17
    assert result["unknown_columns"] == []

    first = result["rows"][0]
    assert first["edge_no"] == "ff4c7557aee1d72b64d70b69f0a3140b"
    assert first["company_name"] == "Lepanto Consolidated Mining Company"
    assert first["company_id"] == "98"
    assert first["template"] == "Material Information/Transactions"
    assert first["pse_form_number"] == "4-30"
    assert first["circular_number"] == "C05754-2026"
    assert first["announced_at"] == datetime(2026, 7, 30, 15, 47, tzinfo=MANILA)


def test_announcements_short_page_has_no_further_pages(announcements_short_html):
    result = parse_disclosure_table(announcements_short_html)
    assert len(result["rows"]) == 36
    assert result["total"] == 36
    assert result["pages"] == 1


def test_empty_result_yields_no_rows_and_does_not_raise(announcements_empty_html):
    result = parse_disclosure_table(announcements_empty_html)
    assert result["rows"] == []
    assert result["total"] == 0


# --- companyDisclosures table (per-company dialect, different column order) ----


def test_company_disclosures_parses_despite_missing_company_column(company_disclosures_html):
    """This dialect drops Company Name and reorders the rest — header-driven mapping
    must still land every field on the right key."""
    result = parse_disclosure_table(company_disclosures_html)

    assert len(result["rows"]) == 50
    assert result["total"] == 343
    assert result["pages"] == 7

    first = result["rows"][0]
    assert first["edge_no"] == "d39af95b65cc2c4464d70b69f0a3140b"
    assert first["template"] == "Share Buy-Back Transactions"
    assert first["pse_form_number"] == "9-1"  # 3rd column here, 3rd in announcements too
    assert first["circular_number"] == "C05694-2026"  # header says "Report or Circular Number"
    assert first["announced_at"] == datetime(2026, 7, 29, 8, 11, tzinfo=MANILA)
    assert first["company_name"] is None  # column absent upstream
    assert first["company_id"] is None


def test_company_disclosures_page_one_is_newest_first(company_disclosures_html):
    """Regression: `sortType=""` made this endpoint return rows in no order at all, so
    page 1 mixed 2024, 2025 and 2026 filings and "recent disclosures" answered with old
    ones. `dateSortType=DESC` alone does not sort it — `sortType=date` is required.
    """
    rows = parse_disclosure_table(company_disclosures_html)["rows"]
    dates = [row["announced_at"] for row in rows]

    assert dates == sorted(dates, reverse=True), "page 1 must be strictly newest-first"
    assert dates[0].year == 2026, "the newest filing should lead, not an arbitrary one"


def test_company_disclosures_last_page_is_short_and_holds_the_oldest(
    company_disclosures_last_page_html,
):
    result = parse_disclosure_table(company_disclosures_last_page_html)
    assert len(result["rows"]) == 43  # 343 total - 6 full pages of 50
    assert result["page"] == 7
    assert result["pages"] == 7

    dates = [row["announced_at"] for row in result["rows"]]
    assert dates == sorted(dates, reverse=True)
    # Descending sort puts the company's earliest filing on the final page.
    assert dates[-1] == datetime(2024, 8, 6, 9, 41, tzinfo=MANILA)


# --- drift detection ----------------------------------------------------------


def test_missing_table_raises_endpoint_changed():
    with pytest.raises(EndpointChangedError):
        parse_disclosure_table("<html><body><p>nothing here</p></body></html>")


def test_table_without_header_raises_endpoint_changed():
    with pytest.raises(EndpointChangedError, match="no header row"):
        parse_disclosure_table("<table class='list'><tbody><tr><td>x</td></tr></tbody></table>")


def test_rows_claimed_but_unparseable_raises_endpoint_changed():
    """Total > 0 with no parseable rows means the row markup moved — fail loudly
    rather than reporting an empty result set."""
    html = """
    <span class="count">[1 / 3] [Total 120]</span>
    <table class="list">
      <thead><tr><th>Company Name</th><th>Template Name</th></tr></thead>
      <tbody><tr><td>Some Corp</td><td><a onclick="newHandler('abc')">Thing</a></td></tr></tbody>
    </table>
    """
    with pytest.raises(EndpointChangedError, match="claims 120 results"):
        parse_disclosure_table(html)


def test_cell_count_mismatch_raises_endpoint_changed():
    html = """
    <table class="list">
      <thead><tr><th>Company Name</th><th>Template Name</th></tr></thead>
      <tbody><tr>
        <td><a onclick="openPopup('ff4c7557aee1d72b64d70b69f0a3140b');return false;">T</a></td>
      </tr></tbody>
    </table>
    """
    with pytest.raises(EndpointChangedError, match="column layout changed"):
        parse_disclosure_table(html)


def test_unknown_column_is_reported_not_fatal():
    """A brand-new column should degrade gracefully — known fields keep working."""
    html = """
    <span class="count">[1 / 1] [Total 1]</span>
    <table class="list">
      <thead><tr><th>Template Name</th><th>Sentiment Score</th></tr></thead>
      <tbody><tr>
        <td><a onclick="openPopup('ff4c7557aee1d72b64d70b69f0a3140b');return false;">Notice</a></td>
        <td>0.42</td>
      </tr></tbody>
    </table>
    """
    result = parse_disclosure_table(html)
    assert result["unknown_columns"] == ["Sentiment Score"]
    assert result["rows"][0]["template"] == "Notice"


# --- keyword full-text search (<dl> dialect) ----------------------------------


def test_keyword_results_parse_hits_with_snippets(keyword_search_html):
    result = parse_keyword_results(keyword_search_html)

    assert result["total"] == 10666
    assert result["pages"] == 1067
    assert len(result["hits"]) == 10  # this endpoint pages at 10, not 50

    first = result["hits"][0]
    assert first["edge_no"] == "8bd8c48799495bb19e4dc6f6c9b65995"
    assert first["subject"] == "Material Information/Transactions"
    assert first["company_name"] == "Petron Corporation"
    assert first["company_id"] == "136"
    assert first["circular_number"] == "C04666-2023"
    assert first["attachment_file_id"] == "1330313"
    assert first["attachment_filename"].endswith(".pdf")
    assert first["announced_at"] == datetime(2023, 6, 15, 9, 11, tzinfo=MANILA)
    assert "dividend" in first["snippet"]


def test_keyword_results_without_dl_raise_endpoint_changed():
    with pytest.raises(EndpointChangedError):
        parse_keyword_results("<span class='count'>[1 / 1] [Total 5]</span><p>moved</p>")


# --- disclosure viewer -------------------------------------------------------


def test_viewer_parses_metadata_documents_and_attachments(disclosure_viewer_html):
    detail = parse_disclosure_viewer(disclosure_viewer_html)

    assert detail["edge_no"] == "ff4c7557aee1d72b64d70b69f0a3140b"
    assert detail["company_name"] == "Lepanto Consolidated Mining Company"
    assert detail["template"] == "Material Information/Transactions"
    assert detail["disclosure_date"] == date(2026, 7, 30)

    assert len(detail["documents"]) == 1
    assert detail["documents"][0]["is_current"] is True

    assert len(detail["attachments"]) == 1
    attachment = detail["attachments"][0]
    assert attachment["file_id"] == "1949133"
    assert attachment["filename"].endswith(".pdf")
    assert attachment["download_url"] == "/downloadFile.do?file_id=1949133"

    # The body lives behind a different file_id than the attachment.
    assert detail["body_file_id"] == "1949127"
    assert detail["body_html_url"] == "/downloadHtml.do?file_id=1949127"


def test_viewer_skips_the_select_prompt_option():
    """#file_list opens with <option value="">Select</option> — not an attachment."""
    html = """
    <div id="viewHeader"><h2>Some Corp</h2><p>Disclosure Date : Jul 30, 2026</p></div>
    <select id="file_list"><option value="">Select</option></select>
    <iframe id="viewContents" src="/downloadHtml.do?file_id=42"></iframe>
    """
    detail = parse_disclosure_viewer(html)
    assert detail["attachments"] == []
    assert detail["body_file_id"] == "42"


def test_viewer_without_header_raises_endpoint_changed():
    with pytest.raises(EndpointChangedError, match="viewHeader"):
        parse_disclosure_viewer("<html><body>gone</body></html>")


# --- client wire format ------------------------------------------------------


@respx.mock
async def test_announcements_sends_form_dialect_with_mmddyyyy_dates(announcements_html):
    route = respx.post(f"{BASE}/announcements/search.ax").mock(
        return_value=httpx.Response(200, text=announcements_html)
    )
    client = make_client()
    await client.search_announcements(
        from_date=date(2026, 7, 1), to_date=date(2026, 7, 30), company_id="599", page=2
    )

    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = dict(pair.split("=", 1) for pair in sent.content.decode().split("&"))
    assert form["fromDate"] == "07-01-2026"
    assert form["toDate"] == "07-30-2026"
    assert form["companyId"] == "599"
    assert form["pageNo"] == "2"
    assert form["dateSortType"] == "DESC"
    await client.aclose()


@respx.mock
async def test_company_disclosures_sends_company_id_as_keyword(company_disclosures_html):
    """The wire param is `keyword`, but it must carry the numeric company id —
    passing a ticker symbol returns zero rows upstream."""
    route = respx.post(f"{BASE}/companyDisclosures/search.ax").mock(
        return_value=httpx.Response(200, text=company_disclosures_html)
    )
    client = make_client()
    await client.search_company_disclosures("599", template="Press Release")

    form = dict(pair.split("=", 1) for pair in route.calls.last.request.content.decode().split("&"))
    assert form["keyword"] == "599"
    assert form["tmplNm"] == "Press+Release"
    # sortType must be the literal "date". Empty leaves rows unordered no matter what
    # dateSortType says, which is what made page 1 lead with 2024 filings.
    assert form["sortType"] == "date"
    assert form["dateSortType"] == "DESC"
    await client.aclose()


@respx.mock
async def test_fulltext_omits_dates_when_not_given(keyword_search_html):
    route = respx.post(f"{BASE}/keyword/search.ax").mock(
        return_value=httpx.Response(200, text=keyword_search_html)
    )
    client = make_client()
    await client.search_disclosure_fulltext("dividend")

    form = dict(pair.split("=", 1) for pair in route.calls.last.request.content.decode().split("&"))
    assert form["keyword"] == "dividend"
    assert form["fromDate"] == ""
    assert form["toDate"] == ""
    await client.aclose()


@respx.mock
async def test_viewer_fetch_passes_edge_no(disclosure_viewer_html):
    route = respx.get(f"{BASE}/openDiscViewer.do").mock(
        return_value=httpx.Response(200, text=disclosure_viewer_html)
    )
    client = make_client()
    html = await client.fetch_disclosure_viewer("ff4c7557aee1d72b64d70b69f0a3140b")
    assert "Lepanto" in html
    assert route.calls.last.request.url.params["edge_no"] == "ff4c7557aee1d72b64d70b69f0a3140b"
    await client.aclose()
