"""Domain-layer tests driven by fakes rather than mocked HTTP.

These exist to exercise decisions the repositories own — endpoint routing, cache-key
construction, exact-symbol matching, URL absolutisation — without a transport in the
picture. That is the payoff of depending on the narrow protocols in `sources.py`: a
fake is a few lines, and a routing bug shows up here rather than being inferred from
which URL respx happened to see.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pse_edge_mcp.errors import InvalidArgumentError, SymbolNotFoundError
from pse_edge_mcp.models import Meta
from pse_edge_mcp.repositories import (
    CompanyRepository,
    DisclosureRepository,
    QuoteRepository,
)
from pse_edge_mcp.service import Served

MNL = ZoneInfo("Asia/Manila")
AS_OF = datetime(2026, 7, 30, 16, 0, tzinfo=MNL)


class FakeCache:
    """A FrozenCache that always fetches and records how it was keyed."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.immutable_flags: list[bool] = []
        self.fetches = 0

    async def get(self, key: str, fetch: Any, *, immutable: bool = False) -> Served[Any]:
        self.keys.append(key)
        self.immutable_flags.append(immutable)
        self.fetches += 1
        value = await fetch()
        return Served(
            value=value,
            meta=Meta(as_of=AS_OF, valid_until=None if immutable else AS_OF, from_cache=False),
        )


class FakeDisclosureSource:
    def __init__(self, html: str) -> None:
        self.html = html
        self.announcement_calls: list[dict[str, Any]] = []
        self.company_calls: list[dict[str, Any]] = []
        self.fulltext_calls: list[dict[str, Any]] = []
        self.viewer_calls: list[str] = []

    async def search_announcements(self, **kwargs: Any) -> str:
        self.announcement_calls.append(kwargs)
        return self.html

    async def search_company_disclosures(self, company_id: str, **kwargs: Any) -> str:
        self.company_calls.append({"company_id": company_id, **kwargs})
        return self.html

    async def search_disclosure_fulltext(self, keyword: str, **kwargs: Any) -> str:
        self.fulltext_calls.append({"keyword": keyword, **kwargs})
        return self.html

    async def fetch_disclosure_viewer(self, edge_no: str) -> str:
        self.viewer_calls.append(edge_no)
        return self.html


SM_AUTOCOMPLETE = [
    {"cmpyId": "154", "cmpyNm": "San Miguel Corporation", "symbol": "SMC", "etfYn": "0"},
    {"cmpyId": "599", "cmpyNm": "SM Investments Corporation", "symbol": "SM", "etfYn": "0"},
    {"cmpyId": "112", "cmpyNm": "SM Prime Holdings, Inc.", "symbol": "SMPH", "etfYn": "0"},
]


class FakeCompanySource:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits
        self.queries: list[str] = []

    async def search_companies(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        return self.hits


# --- company resolution ------------------------------------------------------


async def test_resolve_requires_an_exact_symbol_match():
    """Autocomplete is a prefix search: querying SM also returns SMC and SMPH, and SMC
    sorts first. Taking the first hit would answer about the wrong company."""
    repo = CompanyRepository(FakeCompanySource(SM_AUTOCOMPLETE), FakeCache())

    resolved = await repo.resolve("SM")

    assert resolved.value.symbol == "SM"
    assert resolved.value.company_id == "599"
    assert resolved.value.name == "SM Investments Corporation"


async def test_resolve_is_case_and_whitespace_insensitive():
    repo = CompanyRepository(FakeCompanySource(SM_AUTOCOMPLETE), FakeCache())
    resolved = await repo.resolve("  sm  ")
    assert resolved.value.company_id == "599"


async def test_resolve_without_an_exact_match_raises_symbol_not_found():
    repo = CompanyRepository(FakeCompanySource(SM_AUTOCOMPLETE), FakeCache())
    with pytest.raises(SymbolNotFoundError):
        await repo.resolve("NOPE")


async def test_search_maps_every_hit_and_flags_etfs():
    hits = SM_AUTOCOMPLETE + [
        {"cmpyId": "900", "cmpyNm": "First Metro ETF", "symbol": "FMETF", "etfYn": "1"}
    ]
    repo = CompanyRepository(FakeCompanySource(hits), FakeCache())

    served = await repo.search("sm")

    assert [h.symbol for h in served.value] == ["SMC", "SM", "SMPH", "FMETF"]
    assert served.value[-1].is_etf is True
    assert served.value[0].is_etf is False


async def test_company_lookups_are_cached_under_a_normalised_key():
    """Case differences must not fragment the cache into separate upstream fetches."""
    cache = FakeCache()
    repo = CompanyRepository(FakeCompanySource(SM_AUTOCOMPLETE), cache)

    await repo.resolve("sm")
    await repo.resolve("SM")

    assert cache.keys == ["autocomplete:SM", "autocomplete:SM"]


# --- disclosure routing (the decision this layer owns) -----------------------


async def test_company_without_a_window_routes_to_the_full_history_endpoint(
    company_disclosures_html,
):
    source = FakeDisclosureSource(company_disclosures_html)
    repo = DisclosureRepository(source, FakeCache(), "https://edge.pse.com.ph")

    served = await repo.search(company_id="599", window=None, template="", page=1)

    assert source.company_calls == [{"company_id": "599", "template": "", "page": 1}]
    assert source.announcement_calls == []
    assert served.value.source == "company_disclosures"
    assert served.value.total == 343
    assert served.value.has_more is True


async def test_a_window_routes_to_announcements_and_forwards_the_company_filter(
    announcements_html,
):
    source = FakeDisclosureSource(announcements_html)
    repo = DisclosureRepository(source, FakeCache(), "https://edge.pse.com.ph")

    served = await repo.search(
        company_id="599", window=(date(2026, 7, 1), date(2026, 7, 30)), template="", page=1
    )

    assert source.company_calls == []
    assert source.announcement_calls[0]["company_id"] == "599"
    assert source.announcement_calls[0]["from_date"] == date(2026, 7, 1)
    assert served.value.source == "announcements"


async def test_no_company_and_no_window_is_rejected():
    source = FakeDisclosureSource("")
    repo = DisclosureRepository(source, FakeCache(), "https://edge.pse.com.ph")

    with pytest.raises(InvalidArgumentError):
        await repo.search(company_id=None, window=None, template="", page=1)


async def test_last_page_reports_no_more_pages(company_disclosures_last_page_html):
    source = FakeDisclosureSource(company_disclosures_last_page_html)
    repo = DisclosureRepository(source, FakeCache(), "https://edge.pse.com.ph")

    served = await repo.search(company_id="599", window=None, template="", page=7)

    assert served.value.page == 7
    assert served.value.has_more is False


# --- cache keys --------------------------------------------------------------


async def test_distinct_queries_get_distinct_cache_keys(announcements_html):
    """A key collision here would serve one query's rows for another, which is why key
    construction lives beside the fetch rather than inline at each call site."""
    cache = FakeCache()
    repo = DisclosureRepository(FakeDisclosureSource(announcements_html), cache, "https://x")
    july = (date(2026, 7, 1), date(2026, 7, 30))

    await repo.search(company_id=None, window=july, template="", page=1)
    await repo.search(company_id=None, window=july, template="", page=2)
    await repo.search(company_id="599", window=july, template="", page=1)
    await repo.search(company_id=None, window=july, template="Press Release", page=1)
    await repo.search(
        company_id=None, window=(date(2026, 6, 1), date(2026, 6, 30)), template="", page=1
    )

    assert len(set(cache.keys)) == 5, cache.keys


# --- disclosure detail -------------------------------------------------------


async def test_detail_is_cached_immutably_and_returns_absolute_urls(disclosure_viewer_html):
    cache = FakeCache()
    source = FakeDisclosureSource(disclosure_viewer_html)
    repo = DisclosureRepository(source, cache, "https://edge.pse.com.ph/")

    served = await repo.detail("ff4c7557aee1d72b64d70b69f0a3140b")

    assert cache.immutable_flags == [True]
    assert served.meta.data_policy == "immutable" or served.meta.valid_until is None
    assert (
        served.value.attachments[0].download_url
        == "https://edge.pse.com.ph/downloadFile.do?file_id=1949133"
    )
    assert served.value.body_html_url == "https://edge.pse.com.ph/downloadHtml.do?file_id=1949127"


async def test_trailing_slash_on_base_url_does_not_double_up(disclosure_viewer_html):
    repo = DisclosureRepository(
        FakeDisclosureSource(disclosure_viewer_html), FakeCache(), "https://edge.pse.com.ph/"
    )
    served = await repo.detail("ff4c7557aee1d72b64d70b69f0a3140b")
    assert "//downloadFile" not in served.value.body_html_url  # type: ignore[operator]
    assert served.value.attachments[0].download_url.count("edge.pse.com.ph") == 1


# --- fulltext ----------------------------------------------------------------


async def test_fulltext_forwards_filters_and_carries_the_coverage_note(keyword_search_html):
    source = FakeDisclosureSource(keyword_search_html)
    repo = DisclosureRepository(source, FakeCache(), "https://x")

    served = await repo.fulltext(
        keyword="dividend",
        window=(date(2023, 1, 1), date(2023, 12, 31)),
        company_id="136",
        subject_title="",
        page=1,
    )

    assert source.fulltext_calls[0]["keyword"] == "dividend"
    assert source.fulltext_calls[0]["from_date"] == date(2023, 1, 1)
    assert served.value.total == 10666
    assert "2023-2025" in served.value.coverage_note


# --- quotes ------------------------------------------------------------------


class FakeQuoteSource:
    def __init__(self, html: str, chart: dict[str, Any]) -> None:
        self.html = html
        self.chart = chart
        self.history_calls: list[tuple[str, str, date, date]] = []

    async def fetch_stock_data_page(self, company_id: str) -> str:
        return self.html

    async def fetch_price_history(
        self, company_id: str, security_id: str, start: date, end: date
    ) -> dict[str, Any]:
        self.history_calls.append((company_id, security_id, start, end))
        return self.chart


async def test_history_uses_ids_resolved_from_the_quote_page(stock_data_html, chart_json):
    cache = FakeCache()
    companies = CompanyRepository(FakeCompanySource(SM_AUTOCOMPLETE), cache)
    source = FakeQuoteSource(stock_data_html, chart_json)
    repo = QuoteRepository(source, companies, cache)

    served = await repo.history("SM", date(2026, 6, 1), date(2026, 7, 30))

    company_id, _security_id, start, end = source.history_calls[0]
    assert company_id == "599"  # resolved via exact symbol match, not the first hit
    assert (start, end) == (date(2026, 6, 1), date(2026, 7, 30))
    assert served.value.symbol == "SM"
    assert len(served.value.bars) == len(chart_json["chartData"])
    assert served.value.bars[0].trade_date == date(2026, 7, 28)  # first bar in the fixture


async def test_quote_falls_back_to_autocomplete_identity(stock_data_html, chart_json):
    """The quote page header is the weakest part of the parse, so identity is backfilled
    from autocomplete's clean JSON rather than left blank."""
    cache = FakeCache()
    companies = CompanyRepository(FakeCompanySource(SM_AUTOCOMPLETE), cache)
    repo = QuoteRepository(FakeQuoteSource(stock_data_html, chart_json), companies, cache)

    served = await repo.quote("SM")

    assert served.value.symbol == "SM"
    assert served.value.company_name
    assert served.value.company_id == "599"


# --- duplicate bars (Edge really does this) ----------------------------------


def _chart_row(chart_date: str, close: float = 588.0) -> dict[str, Any]:
    return {
        "CHART_DATE": chart_date,
        "OPEN": 585.0,
        "HIGH": 590.0,
        "LOW": 584.0,
        "CLOSE": close,
        "VALUE": 1.0,
    }


async def test_identical_duplicate_bars_are_collapsed(stock_data_html):
    """Observed live 2026-07-30: Edge's chartData repeated Jul 21 2026 with identical
    values, so a range reported 22 bars for 21 trading days. Identical repeats collapse;
    order and the other days survive."""
    chart = {
        "chartData": [
            _chart_row("Jul 20, 2026 00:00:00", close=580.0),
            _chart_row("Jul 21, 2026 00:00:00"),
            _chart_row("Jul 21, 2026 00:00:00"),  # the duplicate, byte-identical
            _chart_row("Jul 22, 2026 00:00:00", close=591.0),
        ],
        "tableData": [],
    }
    cache = FakeCache()
    companies = CompanyRepository(FakeCompanySource(SM_AUTOCOMPLETE), cache)
    repo = QuoteRepository(FakeQuoteSource(stock_data_html, chart), companies, cache)

    served = await repo.history("SM", date(2026, 7, 20), date(2026, 7, 22))

    dates = [bar.trade_date for bar in served.value.bars]
    assert dates == [date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22)]
    assert served.value.bars[1].close == 588.0


async def test_conflicting_duplicate_bars_raise_endpoint_changed(stock_data_html):
    """The same date with DIFFERENT values is drift we don't understand — invariant #4
    says be loud rather than silently pick one."""
    from pse_edge_mcp.errors import EndpointChangedError

    chart = {
        "chartData": [
            _chart_row("Jul 21, 2026 00:00:00", close=588.0),
            _chart_row("Jul 21, 2026 00:00:00", close=999.0),
        ],
        "tableData": [],
    }
    cache = FakeCache()
    companies = CompanyRepository(FakeCompanySource(SM_AUTOCOMPLETE), cache)
    repo = QuoteRepository(FakeQuoteSource(stock_data_html, chart), companies, cache)

    with pytest.raises(EndpointChangedError, match="appears twice"):
        await repo.history("SM", date(2026, 7, 21), date(2026, 7, 21))
