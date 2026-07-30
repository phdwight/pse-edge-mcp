"""The two scaling behaviours: parse memoisation, and archiving only real fetches.

Both exist because per-request cost was scaling with users while producing an identical
answer for all of them. These tests pin the behaviour that makes that cheap, and the
correctness properties that make it safe.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pse_edge_mcp.memo import ParsedMemo
from pse_edge_mcp.models import Meta
from pse_edge_mcp.repositories import (
    CompanyRepository,
    DisclosureRepository,
    MarketRepository,
    QuoteRepository,
)
from pse_edge_mcp.service import Served

MNL = ZoneInfo("Asia/Manila")
AS_OF = datetime(2026, 7, 30, 16, 30, tzinfo=MNL)


def served(value: Any, *, as_of: datetime = AS_OF, from_cache: bool = False) -> Served[Any]:
    return Served(
        value=value,
        meta=Meta(as_of=as_of, valid_until=as_of + timedelta(days=1), from_cache=from_cache),
    )


# --- the memo ----------------------------------------------------------------


def test_memo_parses_once_while_the_underlying_value_is_unchanged():
    memo = ParsedMemo()
    calls = 0

    def parse(html: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"parsed": html}

    for _ in range(50):
        result = memo.resolve("k#proj", served("<html/>", from_cache=True), parse)
        assert result.value == {"parsed": "<html/>"}

    assert calls == 1, "50 requests over frozen data must parse once"
    assert memo.stats() == {"hits": 49, "misses": 1, "entries": 1}


def test_memo_reparses_when_as_of_moves():
    """`as_of` is the validity token: a new upstream fetch necessarily carries a new one,
    so a boundary crossing invalidates the memo with no explicit invalidation logic."""
    memo = ParsedMemo()
    calls = 0

    def parse(html: str) -> str:
        nonlocal calls
        calls += 1
        return html.upper()

    memo.resolve("k#proj", served("first"), parse)
    memo.resolve("k#proj", served("first"), parse)
    assert calls == 1

    later = memo.resolve("k#proj", served("second", as_of=AS_OF + timedelta(days=1)), parse)
    assert calls == 2
    assert later.value == "SECOND"


def test_memo_never_invents_metadata():
    """Reuse is a local optimisation and must not change what the caller is told about
    freshness — `from_cache` and `stale` describe the upstream fetch, not this memo."""
    memo = ParsedMemo()
    memo.resolve("k#proj", served("html", from_cache=False), lambda h: h)

    reused = memo.resolve("k#proj", served("html", from_cache=True), lambda h: h)
    assert reused.meta.from_cache is True
    assert reused.meta.as_of == AS_OF


def test_memo_evicts_least_recently_used_and_stays_bounded():
    """Cache keys are unbounded (per symbol, page, date range), so the memo must not be."""
    memo = ParsedMemo(max_entries=3)
    for i in range(10):
        memo.resolve(f"key{i}", served(f"v{i}"), lambda h: h)
    assert memo.stats()["entries"] == 3

    memo = ParsedMemo(max_entries=2)
    memo.resolve("a", served("a"), lambda h: h)
    memo.resolve("b", served("b"), lambda h: h)
    memo.resolve("a", served("a"), lambda h: h)  # touch 'a' so 'b' is now oldest
    memo.resolve("c", served("c"), lambda h: h)
    calls = 0

    def counting(h: str) -> str:
        nonlocal calls
        calls += 1
        return h

    memo.resolve("a", served("a"), counting)
    assert calls == 0, "'a' was recently used and must have survived"
    memo.resolve("b", served("b"), counting)
    assert calls == 1, "'b' was least recently used and must have been evicted"


# --- the collision that would be silent --------------------------------------


class FakeMarketSource:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls = 0

    async def fetch_homepage(self) -> str:
        self.calls += 1
        return self.html


class RecordingCache:
    """Serves from a dict, reporting from_cache like FreezeService does."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.fetches = 0

    async def get(self, key: str, fetch: Any, *, immutable: bool = False) -> Served[Any]:
        if key in self.store:
            return served(self.store[key], from_cache=True)
        self.fetches += 1
        self.store[key] = await fetch()
        return served(self.store[key], from_cache=False)


async def test_indices_and_summary_do_not_collide_in_the_memo(homepage_html):
    """Both read the SAME cached homepage under one cache key but parse it into different
    shapes. A memo keyed only on the cache key would hand one tool the other's result —
    a silent, badly-wrong answer, so it is pinned here.
    """
    memo = ParsedMemo()
    source = FakeMarketSource(homepage_html)
    repo = MarketRepository(source, RecordingCache(), memo)

    indices = await repo.indices()
    summary = await repo.summary()
    indices_again = await repo.indices()

    assert len(indices.value.indices) == 8
    assert len(summary.value.indices) == 8
    assert summary.value.feeds, "summary must carry feeds"
    assert not hasattr(indices.value, "feeds"), "indices must not be a MarketSummary"
    assert indices_again.value.indices[0].name == indices.value.indices[0].name
    # One upstream fetch, and each projection parsed once.
    assert source.calls == 1
    assert memo.stats()["misses"] == 2
    assert memo.stats()["hits"] == 1


async def test_memo_is_shared_so_two_tools_reuse_one_parse(homepage_html):
    """summary() called twice must parse once even though indices() ran in between."""
    memo = ParsedMemo()
    repo = MarketRepository(FakeMarketSource(homepage_html), RecordingCache(), memo)

    await repo.summary()
    await repo.indices()
    await repo.summary()

    assert memo.stats()["misses"] == 2, "one parse per projection"


# --- archiving only on real fetches ------------------------------------------


class CountingArchive:
    def __init__(self) -> None:
        self.disclosure_batches: list[int] = []
        self.bar_batches: list[int] = []

    async def record_bars(self, *, company_id, security_id, symbol, bars) -> None:
        self.bar_batches.append(len(bars))

    async def record_disclosures(self, hits) -> None:
        self.disclosure_batches.append(len(hits))


class FakeDisclosureSource:
    def __init__(self, html: str) -> None:
        self.html = html

    async def search_announcements(self, **kwargs: Any) -> str:
        return self.html

    async def search_company_disclosures(self, company_id: str, **kwargs: Any) -> str:
        return self.html

    async def search_disclosure_fulltext(self, keyword: str, **kwargs: Any) -> str:
        return self.html

    async def fetch_disclosure_viewer(self, edge_no: str) -> str:
        return self.html


async def test_repeated_searches_archive_once_not_once_per_request(announcements_html):
    """Regression: archiving ran unconditionally, so a cached search still wrote up to 50
    rows per request. At 1000 req/s that is ~50k no-op upserts per second of pure write
    churn — it would dominate database load while adding no information."""
    archive = CountingArchive()
    repo = DisclosureRepository(
        FakeDisclosureSource(announcements_html),
        RecordingCache(),
        "https://edge.pse.com.ph",
        archive,
        ParsedMemo(),
    )
    window = (date(2026, 7, 1), date(2026, 7, 30))

    for _ in range(20):
        result = await repo.search(company_id=None, window=window, template="", page=1)
        assert len(result.value.hits) == 50, "every caller still gets the full page"

    assert archive.disclosure_batches == [50], "archived on the first fetch only"


async def test_a_new_query_still_archives(announcements_html):
    """The guard must key off cache state, not suppress archiving generally."""
    archive = CountingArchive()
    repo = DisclosureRepository(
        FakeDisclosureSource(announcements_html),
        RecordingCache(),
        "https://x",
        archive,
        ParsedMemo(),
    )

    await repo.search(
        company_id=None, window=(date(2026, 7, 1), date(2026, 7, 30)), template="", page=1
    )
    await repo.search(
        company_id=None, window=(date(2026, 7, 1), date(2026, 7, 30)), template="", page=2
    )
    await repo.search(
        company_id=None, window=(date(2026, 6, 1), date(2026, 6, 30)), template="", page=1
    )

    assert len(archive.disclosure_batches) == 3, "each distinct query is a real fetch"


class FakeQuoteSource:
    def __init__(self, html: str, chart: dict[str, Any]) -> None:
        self.html = html
        self.chart = chart

    async def fetch_stock_data_page(self, company_id: str) -> str:
        return self.html

    async def fetch_price_history(self, company_id, security_id, start, end) -> dict[str, Any]:
        return self.chart


class FakeCompanySource:
    async def search_companies(self, query: str) -> list[dict[str, Any]]:
        return [
            {"cmpyId": "599", "cmpyNm": "SM Investments Corporation", "symbol": "SM", "etfYn": "0"}
        ]


async def test_repeated_price_history_archives_bars_once(stock_data_html, chart_json):
    archive = CountingArchive()
    cache = RecordingCache()
    memo = ParsedMemo()
    companies = CompanyRepository(FakeCompanySource(), cache)
    repo = QuoteRepository(
        FakeQuoteSource(stock_data_html, chart_json), companies, cache, archive, memo
    )

    for _ in range(10):
        result = await repo.history("SM", date(2026, 7, 1), date(2026, 7, 30))
        assert len(result.value.bars) == len(chart_json["chartData"])

    assert archive.bar_batches == [len(chart_json["chartData"])], "bars archived once"


@pytest.mark.parametrize("from_cache", [False, True])
async def test_callers_get_identical_data_whether_archived_or_not(announcements_html, from_cache):
    """Skipping the archive write must not change a single byte of what the caller sees."""
    cache = RecordingCache()
    repo = DisclosureRepository(
        FakeDisclosureSource(announcements_html),
        cache,
        "https://x",
        CountingArchive(),
        ParsedMemo(),
    )
    window = (date(2026, 7, 1), date(2026, 7, 30))

    first = await repo.search(company_id=None, window=window, template="", page=1)
    if not from_cache:
        assert first.meta.from_cache is False
        return
    second = await repo.search(company_id=None, window=window, template="", page=1)
    assert second.meta.from_cache is True
    assert second.value.model_dump() == first.value.model_dump()
