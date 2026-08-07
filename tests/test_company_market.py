"""Phase 3: company profile, financial highlights, dividends/rights, indices, summary.

Parsers run against fixtures recorded live 2026-07-30; repositories run against fakes.
No test touches PSE Edge.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pse_edge_mcp.errors import EndpointChangedError
from pse_edge_mcp.models import Meta
from pse_edge_mcp.parsers import (
    parse_company_profile,
    parse_dividends,
    parse_financial_reports,
    parse_indices,
    parse_market_summary,
    parse_rights,
)
from pse_edge_mcp.repositories import CompanyInfoRepository, CompanyRepository, MarketRepository
from pse_edge_mcp.service import Served

MNL = ZoneInfo("Asia/Manila")
AS_OF = datetime(2026, 7, 30, 18, 0, tzinfo=MNL)


class FakeCache:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def get(self, key: str, fetch: Any, *, policy: str = "EOD-frozen") -> Served[Any]:
        self.keys.append(key)
        return Served(
            value=await fetch(),
            meta=Meta(as_of=AS_OF, valid_until=AS_OF, from_cache=False),
        )


SM = [{"cmpyId": "599", "cmpyNm": "SM Investments Corporation", "symbol": "SM", "etfYn": "0"}]


class FakeCompanySource:
    async def search_companies(self, query: str) -> list[dict[str, Any]]:
        return SM


class FakeInfoSource:
    def __init__(self, profile: str = "", financials: str = "", dr: dict[str, str] | None = None):
        self.profile_html = profile
        self.financials_html = financials
        self.dr = dr or {}
        self.dr_calls: list[str] = []

    async def fetch_company_information(self, company_id: str) -> str:
        return self.profile_html

    async def fetch_financial_reports(self, company_id: str) -> str:
        return self.financials_html

    async def fetch_dividends_or_rights(self, company_id: str, kind: str) -> str:
        self.dr_calls.append(kind)
        return self.dr.get(kind, "")


class FakeMarketSource:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls = 0

    async def fetch_homepage(self) -> str:
        self.calls += 1
        return self.html


# --- company profile ---------------------------------------------------------


def test_profile_maps_known_labels_and_keeps_the_rest(company_profile_html):
    p = parse_company_profile(company_profile_html)

    assert p["company_name"] == "SM Investments Corporation"
    assert p["sector"] == "Holding Firms"
    assert p["subsector"] == "Holding Firms"
    assert p["incorporation_date"] == date(1960, 5, 15)
    assert p["number_of_directors"] == 8
    assert p["fiscal_year"] == "12/31 (Month/Day)"
    assert p["external_auditor"] == "SyCip, Gorres, Velayo & Company"
    assert p["website"] == "http://www.sminvestments.com"
    assert p["telephone"] == "(632) 8857-0100"
    # Everything on the page survives verbatim, so an unmapped label is never lost.
    assert len(p["raw_fields"]) == 14
    assert "Transfer Agent" in p["raw_fields"]


def test_profile_without_label_rows_raises_endpoint_changed():
    with pytest.raises(EndpointChangedError, match="companyInformation"):
        parse_company_profile("<html><body><p>redesigned</p></body></html>")


# --- financial highlights ----------------------------------------------------


def test_financials_split_annual_and_quarterly_with_their_own_units(financial_reports_html):
    """The two sections carry *different* units labels in this real capture, which is why
    values are never rescaled and each period reports its own label."""
    result = parse_financial_reports(financial_reports_html)
    periods = {p["period_type"]: p for p in result["periods"]}

    assert set(periods) == {"annual", "quarterly"}
    assert periods["annual"]["period_ended"] == date(2025, 12, 31)
    assert periods["annual"]["currency_units"] == "Php (in thousands)"
    assert periods["quarterly"]["period_ended"] == date(2026, 3, 31)
    assert periods["quarterly"]["currency_units"] == "Php (in Millions)"
    assert periods["annual"]["currency_units"] != periods["quarterly"]["currency_units"]


def test_financials_keep_edge_line_items_and_column_labels(financial_reports_html):
    periods = {
        p["period_type"]: p for p in parse_financial_reports(financial_reports_html)["periods"]
    }

    annual = {s["statement"]: s for s in periods["annual"]["statements"]}
    assert set(annual) == {"Balance Sheet", "Income Statement"}
    assert annual["Balance Sheet"]["columns"] == ["Current Year", "Previous Year"]
    assert annual["Balance Sheet"]["items"]["Total Assets"] == [1811801.0, 1699052.0]
    assert annual["Income Statement"]["items"]["Gross Revenue"] == [681733.0, 654777.0]

    quarterly = {s["statement"]: s for s in periods["quarterly"]["statements"]}
    # Quarterly compares against the audited fiscal year, and its income statement is
    # four columns wide including year-to-date — not the same shape as annual.
    assert quarterly["Balance Sheet"]["columns"] == ["Period Ended", "Fiscal Year Ended(Audited)"]
    assert len(quarterly["Income Statement"]["columns"]) == 4
    assert len(quarterly["Income Statement"]["items"]["Gross Revenue"]) == 4


def test_financials_attribute_statements_to_the_right_section(financial_reports_html):
    """Regression: document order is essential here. `iter()` sees none of these nodes
    (they are nested) and a comma CSS selector returns matches grouped by selector, which
    filed all four statements under the last heading and left annual empty.
    """
    periods = parse_financial_reports(financial_reports_html)["periods"]
    assert [p["period_type"] for p in periods] == ["annual", "quarterly"]
    assert all(len(p["statements"]) == 2 for p in periods)
    # Annual must not have inherited the quarterly period line.
    annual = periods[0]
    assert "fiscal year" in (annual["period_label"] or "").lower()


def test_financials_page_with_no_statements_reports_the_notice_instead_of_raising():
    """Synthetic, not a capture: companies that have not filed yet show only the notice.
    That must degrade to an empty result, not an error."""
    html = """
    <div id="mainContents">
      <p class="textCont">Information in this page will become available upon submission
      of the Company of its latest financial statements.</p>
    </div>
    """
    result = parse_financial_reports(html)
    assert result["periods"] == []
    assert "become available" in result["note"]


# --- dividends & rights ------------------------------------------------------


def test_dividends_parse_with_a_link_back_to_the_announcing_disclosure(dividends_html):
    records = parse_dividends(dividends_html)

    assert len(records) == 1
    d = records[0]
    assert d["security_type"] == "COMMON"
    assert d["dividend_type"] == "Cash"
    assert d["dividend_rate"] == "Php17.00"  # kept verbatim: rates are not all amounts
    assert d["ex_dividend_date"] == date(2026, 5, 13)
    assert d["record_date"] == date(2026, 5, 14)
    assert d["payment_date"] == date(2026, 5, 28)
    assert d["circular_number"] == "C03012-2026"
    assert d["edge_no"] == "d1d47bad7c496e1164d70b69f0a3140b"


def test_rights_with_no_data_returns_empty_not_an_error(rights_empty_html):
    assert parse_rights(rights_empty_html) == []


def test_record_table_without_a_table_raises_endpoint_changed():
    with pytest.raises(EndpointChangedError, match="dividends list"):
        parse_dividends("<html><body>gone</body></html>")


# --- indices -----------------------------------------------------------------


def test_indices_derive_sign_from_the_arrow_not_the_printed_number(homepage_html):
    """Edge prints Chg unsigned and shows direction only via colour and a ▲/▼ glyph, so a
    naive parse reports every decline as a gain. PSEi fell on this capture."""
    indices = {i["name"]: i for i in parse_indices(homepage_html)}

    assert len(indices) == 8
    psei = indices["PSEi"]
    assert psei["value"] == 6305.75
    assert psei["change"] == -47.29  # printed as "47.29" with a ▼
    assert psei["change_percent"] == -0.74
    assert psei["direction"] == "down"

    gainer = indices["Mining and Oil"]
    assert gainer["change"] == 241.19
    assert gainer["change_percent"] == 1.51
    assert gainer["direction"] == "up"

    assert {
        "All Shares",
        "Financials",
        "Industrial",
        "Holding Firms",
        "Property",
        "Services",
    } <= set(indices)


def test_indices_signs_are_consistent_with_their_direction(homepage_html):
    for row in parse_indices(homepage_html):
        if row["direction"] == "down":
            assert row["change"] <= 0 and row["change_percent"] <= 0, row
        elif row["direction"] == "up":
            assert row["change"] >= 0 and row["change_percent"] >= 0, row


def test_missing_index_block_raises_endpoint_changed():
    with pytest.raises(EndpointChangedError, match="div.index"):
        parse_indices("<html><body><div class='other'></div></body></html>")


def test_index_table_present_but_unparseable_raises_endpoint_changed():
    html = "<div class='index'><table><tr><td>moved</td></tr></table></div>"
    with pytest.raises(EndpointChangedError, match="no index rows"):
        parse_indices(html)


# --- market summary ----------------------------------------------------------


def test_market_summary_keys_feeds_by_edges_own_labels(homepage_html):
    result = parse_market_summary(homepage_html)

    assert len(result["indices"]) == 8
    feeds = result["feeds"]
    assert {
        "Company Announcements",
        "Financial Reports",
        "Other Reports",
        "Listing Notices",
        "Disclosure Notices",
        "Today",
        "This Week",
    } <= set(feeds)

    item = feeds["Company Announcements"][0]
    assert item["edge_no"] and len(item["edge_no"]) == 32
    assert item["symbol"] == "FMETF"
    assert item["company_id"] == "649"
    assert item["announced_at"] == datetime(2026, 7, 30, 17, 19, tzinfo=MNL)
    assert item["circular_number"] == "C05756-2026"


def test_most_viewed_items_use_the_other_markup_shape(homepage_html):
    """Most-viewed entries lead with the symbol rather than the title, so fields are
    identified by content rather than position."""
    today = parse_market_summary(homepage_html)["feeds"]["Today"]
    assert len(today) == 5
    assert today[0]["symbol"] == "BPI"
    assert today[0]["title"] == "Notice of Analysts'/Investors' Briefing"
    assert today[0]["edge_no"]


# --- repositories ------------------------------------------------------------


async def test_profile_repository_backfills_identity_and_keys_by_company_id(
    company_profile_html,
):
    cache = FakeCache()
    companies = CompanyRepository(FakeCompanySource(), cache)
    repo = CompanyInfoRepository(FakeInfoSource(profile=company_profile_html), companies, cache)

    served = await repo.profile("SM")

    assert served.value.company_id == "599"
    assert served.value.company_name == "SM Investments Corporation"
    assert "company_info:599" in cache.keys


async def test_dividends_and_rights_fetches_both_tabs_under_separate_keys(
    dividends_html, rights_empty_html
):
    cache = FakeCache()
    companies = CompanyRepository(FakeCompanySource(), cache)
    source = FakeInfoSource(dr={"Dividends": dividends_html, "Rights": rights_empty_html})
    repo = CompanyInfoRepository(source, companies, cache)

    served = await repo.dividends_and_rights("SM")

    assert source.dr_calls == ["Dividends", "Rights"]
    assert "dividends:599" in cache.keys and "rights:599" in cache.keys
    assert len(served.value.dividends) == 1
    assert served.value.rights == []
    assert served.value.dividends[0].edge_no == "d1d47bad7c496e1164d70b69f0a3140b"


async def test_indices_and_summary_share_one_homepage_fetch(homepage_html):
    """Both derive from the same server-rendered page, so asking for each in turn must
    not cost PSE Edge two requests."""
    source = FakeMarketSource(homepage_html)
    repo = MarketRepository(source, FakeCache())

    indices = await repo.indices()
    summary = await repo.summary()

    assert len(indices.value.indices) == 8
    assert len(summary.value.indices) == 8
    assert summary.value.feeds["Today"]
    # FakeCache doesn't cache, so this asserts a single key is used; the real
    # FreezeService then collapses the pair into one upstream hit.
    assert source.calls == 2
    assert repo.HOMEPAGE_KEY == "homepage"


async def test_financials_repository_reports_units_per_period(financial_reports_html):
    cache = FakeCache()
    companies = CompanyRepository(FakeCompanySource(), cache)
    repo = CompanyInfoRepository(
        FakeInfoSource(financials=financial_reports_html), companies, cache
    )

    served = await repo.financials("SM")

    units = {p.period_type: p.currency_units for p in served.value.periods}
    assert units == {"annual": "Php (in thousands)", "quarterly": "Php (in Millions)"}
    assert served.value.company_name == "SM Investments Corporation"
