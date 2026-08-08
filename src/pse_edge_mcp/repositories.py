"""Domain layer: one repository per data domain.

Each repository owns the full path from a domain question to a validated model —
cache key, freeze-policy read, HTML/JSON parse, model construction — for exactly one
domain. That placement is the point:

- **Single responsibility:** a cache key now lives beside the fetch it identifies.
  When these were built inline in the tool functions, a typo produced a silent cache
  miss or, worse, a collision between two different queries.
- **Open/closed:** a new data domain (financial reports, dividends, indices)
  is a new repository. Adding one does not touch existing repositories or the tools.
- **Dependency inversion:** repositories depend on the narrow protocols in `sources.py`
  and on `FrozenCache`, never on `PseEdgeClient` or `FreezeService` concretely, so they
  can be exercised with small fakes and no HTTP.

Repositories return `Served[Model]`, so freshness metadata travels with the data and
cannot be dropped on the way to the caller.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any, Literal

from .archive import Archive, NullArchive
from .errors import EndpointChangedError, InvalidArgumentError, SymbolNotFoundError
from .memo import ParsedMemo
from .models import (
    CompanyHit,
    CompanyProfile,
    DisclosureDetail,
    DisclosureHit,
    DisclosureSearchResult,
    DividendRecord,
    DividendsAndRights,
    FeedItem,
    FinancialHighlights,
    FinancialPeriod,
    IndexQuote,
    KeywordHit,
    KeywordSearchResult,
    MarketIndices,
    MarketSummary,
    OhlcBar,
    PriceHistory,
    RightsRecord,
    StockQuote,
)
from .parsers import (
    parse_chart_date,
    parse_company_profile,
    parse_disclosure_table,
    parse_disclosure_viewer,
    parse_dividends,
    parse_financial_reports,
    parse_indices,
    parse_keyword_results,
    parse_market_summary,
    parse_rights,
    parse_stock_data_page,
)
from .service import FrozenCache, Served
from .sources import (
    CompanyInfoSource,
    CompanySource,
    DisclosureSource,
    MarketSource,
    QuoteSource,
)

SearchSource = Literal["announcements", "company_disclosures"]


def _company_hit(raw: dict[str, Any]) -> CompanyHit:
    """Autocomplete's wire shape -> our model. One place, so search and resolve agree."""
    return CompanyHit(
        company_id=raw["cmpyId"],
        name=raw.get("cmpyNm", ""),
        symbol=raw["symbol"],
        is_etf=raw.get("etfYn") == "1",
    )


def _dedupe_bars(bars: Iterable[OhlcBar]) -> list[OhlcBar]:
    """Collapse repeated trade dates, loudly if they disagree.

    Edge's own chartData genuinely repeats days — observed live 2026-07-30: Jul 21 2026
    appeared twice with identical values, so a range reported 22 bars for 21 trading days
    and a day-counting or averaging consumer would silently double-count. An identical
    repeat is an upstream quirk and is collapsed; the same date with *different* values is
    data drift we don't understand, and invariant #4 says be loud, not guess.
    """
    seen: dict[date, OhlcBar] = {}
    for bar in bars:
        previous = seen.get(bar.trade_date)
        if previous is None:
            seen[bar.trade_date] = bar
        elif previous != bar:
            raise EndpointChangedError(
                f"DisclosureCht.ax: {bar.trade_date.isoformat()} appears twice with "
                f"different values — upstream data changed shape"
            )
    return list(seen.values())


def _has_more(page: int, pages: int | None) -> bool:
    """Upstream reports exact totals, so "is there another page" is arithmetic.

    Shared because both paginated result types answer it identically, and answering it
    two different ways is how they would eventually disagree.
    """
    return bool(pages and page < pages)


class CompanyRepository:
    """Symbol/name lookup — the entry point every other domain depends on."""

    def __init__(self, source: CompanySource, cache: FrozenCache) -> None:
        self._source = source
        self._cache = cache

    async def search(self, query: str) -> Served[list[CompanyHit]]:
        served = await self._raw(query)
        return served.map(lambda hits: [_company_hit(h) for h in hits])

    async def resolve(self, symbol: str) -> Served[CompanyHit]:
        """Resolve a ticker to its company, requiring an exact symbol match.

        Autocomplete is a prefix search: "SM" also returns SMC and SMPH, so accepting
        the first hit would silently answer about the wrong company.
        """
        served = await self.try_resolve(symbol)
        if served.value is None:
            raise SymbolNotFoundError(
                f"No PSE-listed company found for symbol '{symbol.strip().upper()}'"
            )
        return Served(value=served.value, meta=served.meta)

    async def try_resolve(self, symbol: str) -> Served[CompanyHit | None]:
        """Same exact match, but "no such symbol" is an answer rather than an error.

        `validate_symbol` needs to say `valid: false` where every other tool needs to
        fail, and the matching rule must not be written twice: two implementations of
        "is this the right company" would eventually disagree, and the one that drifted
        would answer about the wrong company rather than failing visibly.
        """
        wanted = symbol.strip().upper()
        served = await self._raw(wanted)
        for hit in served.value:
            if hit.get("symbol", "").upper() == wanted:
                # Built eagerly rather than through `.map`, so nothing closes over the
                # loop variable.
                return Served(value=_company_hit(hit), meta=served.meta)
        return Served(value=None, meta=served.meta)

    async def _raw(self, query: str) -> Served[list[dict[str, Any]]]:
        normalised = query.strip().upper()
        return await self._cache.get(
            f"autocomplete:{normalised}",
            lambda: self._source.search_companies(query.strip()),
            policy="daily-refresh",
        )


class QuoteRepository:
    """Quotes and OHLC history. Both need a security_id, which only the quote page has."""

    def __init__(
        self,
        source: QuoteSource,
        companies: CompanyRepository,
        cache: FrozenCache,
        archive: Archive | None = None,
        memo: ParsedMemo | None = None,
    ) -> None:
        self._source = source
        self._companies = companies
        self._cache = cache
        # NullArchive by default, so stdio use needs no database and nothing above this
        # layer has to know whether an archive exists.
        self._archive = archive or NullArchive()
        self._memo = memo or ParsedMemo()

    SESSION_QUOTE_NOTE = (
        "The market is open and this symbol had no cached end-of-day record, so only "
        "previous_close — the last settled price before this session — is provided. "
        "This is not a realtime quote; full end-of-day figures arrive after the "
        "15:00 Manila close."
    )

    async def quote(self, symbol: str) -> Served[StockQuote]:
        parsed = await self._quote_page(symbol)
        served = parsed.map(lambda fields: StockQuote(**fields))
        if served.meta.note is None:
            return served
        # A mid-session snapshot (meta.note is set exactly for those — see
        # FreezeService._served). The page's session fields are delayed, moving values;
        # surfacing them would present an intraday number as a price. Only
        # previous_close is a settled figure — precisely "the value before the market
        # opened" — so identity plus previous_close is everything the tool returns,
        # and the note says so.
        trimmed = served.map(_previous_close_only)
        return Served(
            value=trimmed.value,
            meta=trimmed.meta.model_copy(update={"note": self.SESSION_QUOTE_NOTE}),
        )

    async def history(self, symbol: str, start: date, end: date) -> Served[PriceHistory]:
        parsed = await self._quote_page(symbol)
        company_id = parsed.value["company_id"]
        security_id = parsed.value["security_id"]

        key = f"ohlc:{company_id}:{security_id}:{start.isoformat()}:{end.isoformat()}"
        # Price data: the one domain that keeps the full market-boundary freeze.
        served = await self._cache.get(
            key,
            lambda: self._source.fetch_price_history(company_id, security_id, start, end),
            policy="EOD-frozen",
        )

        def build(data: dict[str, Any]) -> PriceHistory:
            return PriceHistory(
                symbol=parsed.value["symbol"],
                company_id=company_id,
                security_id=security_id,
                start_date=start,
                end_date=end,
                bars=_dedupe_bars(
                    OhlcBar(
                        trade_date=parse_chart_date(row["CHART_DATE"]),
                        open=row["OPEN"],
                        high=row["HIGH"],
                        low=row["LOW"],
                        close=row["CLOSE"],
                        value=row["VALUE"],
                    )
                    for row in data["chartData"]
                ),
            )

        history = self._memo.resolve(f"{key}#history", served, build)
        # Opportunistic archive (plan §6a): bars fetched for this caller are recorded on
        # the way past, so history deepens without a single extra request to PSE Edge.
        # Failures are logged, never raised — see archive.py.
        #
        # Only on a genuine upstream fetch. A cache hit carries nothing new, so archiving
        # it would re-write the same rows on every request — up to 50 no-op upserts per
        # call — and that write churn would dominate database load under real traffic
        # while adding zero information.
        if not served.meta.from_cache:
            await self._archive.record_bars(
                company_id=company_id,
                security_id=security_id,
                symbol=history.value.symbol,
                bars=history.value.bars,
            )
        return history

    async def _quote_page(self, symbol: str) -> Served[dict[str, Any]]:
        """Fetch and parse the quote page, backfilling identity from autocomplete.

        The page's own header is the weakest part of the parse, so name/symbol fall back
        to the autocomplete result, which is clean JSON.
        """
        company = await self._companies.resolve(symbol)
        company_id = company.value.company_id
        # Price data: the one domain that keeps the full market-boundary freeze.
        served = await self._cache.get(
            f"stock_data:{company_id}",
            lambda: self._source.fetch_stock_data_page(company_id),
            policy="EOD-frozen",
        )
        return self._memo.resolve(
            f"stock_data:{company_id}#fields",
            served,
            lambda html: _quote_fields(html, company.value),
        )


def _cap_attachments(detail: DisclosureDetail, max_files: int) -> DisclosureDetail:
    """Return at most `max_files` attachments, honestly labelled.

    Applied after the memo so the cached model stays complete — the cap is a response-size
    guard, not a cache policy — and `attachments_total`/`attachments_truncated` always say
    what was withheld rather than letting a shortened list read as the whole set.
    """
    if len(detail.attachments) <= max_files:
        return detail
    return detail.model_copy(
        update={"attachments": detail.attachments[:max_files], "attachments_truncated": True}
    )


def _previous_close_only(quote: StockQuote) -> StockQuote:
    """Reduce a quote to identity + previous_close, dropping every session-moving field.

    Built as a fresh model rather than by clearing attributes so a newly added
    StockQuote field defaults to None here instead of silently leaking a mid-session
    value. raw_fields is emptied for the same reason — it carries the whole page.
    """
    return StockQuote(
        symbol=quote.symbol,
        company_name=quote.company_name,
        company_id=quote.company_id,
        security_id=quote.security_id,
        previous_close=quote.previous_close,
    )


def _quote_fields(html: str, company: CompanyHit) -> dict[str, Any]:
    parsed = parse_stock_data_page(html)
    parsed["symbol"] = parsed.get("symbol") or company.symbol
    parsed["company_name"] = parsed.get("company_name") or company.name
    return parsed


class DisclosureRepository:
    """Disclosure search and detail.

    Which upstream endpoint serves a search is a domain decision, not a transport or
    presentation one, so the routing lives here (see docs/endpoints.md v3).
    """

    def __init__(
        self,
        source: DisclosureSource,
        cache: FrozenCache,
        base_url: str,
        archive: Archive | None = None,
        memo: ParsedMemo | None = None,
    ) -> None:
        self._source = source
        self._cache = cache
        self._base_url = base_url.rstrip("/")
        self._archive = archive or NullArchive()
        self._memo = memo or ParsedMemo()

    async def search(
        self,
        *,
        company_id: str | None,
        window: tuple[date, date] | None,
        template: str,
        page: int,
    ) -> Served[DisclosureSearchResult]:
        """A company with no date window wants its whole history, which only
        companyDisclosures can serve; everything else is a date-ranged announcements
        query."""
        source_name: SearchSource
        if company_id and window is None:
            key = f"company_disclosures:{company_id}:{template}:{page}"
            served = await self._cache.get(
                key,
                lambda: self._source.search_company_disclosures(
                    company_id, template=template, page=page
                ),
                policy="daily-refresh",
            )
            source_name = "company_disclosures"
        elif window is not None:
            start, end = window
            key = (
                f"announcements:{company_id or ''}:{start.isoformat()}:"
                f"{end.isoformat()}:{template}:{page}"
            )
            served = await self._cache.get(
                key,
                lambda: self._source.search_announcements(
                    from_date=start,
                    to_date=end,
                    company_id=company_id or "",
                    template=template,
                    page=page,
                ),
                policy="daily-refresh",
            )
            source_name = "announcements"
        else:
            raise InvalidArgumentError("a date range is required when no symbol is given")

        def build(html: str) -> DisclosureSearchResult:
            parsed = parse_disclosure_table(html)
            reported_page = parsed.get("page") or page
            return DisclosureSearchResult(
                hits=[DisclosureHit(**row) for row in parsed["rows"]],
                page=reported_page,
                pages=parsed.get("pages"),
                total=parsed.get("total"),
                has_more=_has_more(reported_page, parsed.get("pages")),
                source=source_name,
            )

        result = self._memo.resolve(f"{key}#search", served, build)
        # Archived only on a genuine upstream fetch. Previously this ran unconditionally,
        # so a cached search still wrote up to 50 rows per request — write churn that would
        # dominate database load at scale while adding nothing.
        if not served.meta.from_cache:
            await self._archive.record_disclosures(result.value.hits)
        return result

    async def fulltext(
        self,
        *,
        keyword: str,
        window: tuple[date | None, date | None],
        company_id: str,
        subject_title: str,
        page: int,
    ) -> Served[KeywordSearchResult]:
        start, end = window
        key = f"keyword:{keyword}:{start}:{end}:{company_id}:{subject_title}:{page}"
        served = await self._cache.get(
            key,
            lambda: self._source.search_disclosure_fulltext(
                keyword,
                from_date=start,
                to_date=end,
                company_id=company_id,
                subject_title=subject_title,
                page=page,
            ),
            policy="daily-refresh",
        )

        def build(html: str) -> KeywordSearchResult:
            parsed = parse_keyword_results(html)
            reported_page = parsed.get("page") or page
            return KeywordSearchResult(
                hits=[KeywordHit(**hit) for hit in parsed["hits"]],
                page=reported_page,
                pages=parsed.get("pages"),
                total=parsed.get("total"),
                has_more=_has_more(reported_page, parsed.get("pages")),
            )

        return self._memo.resolve(f"{key}#fulltext", served, build)

    #: Attachment cap when the caller does not name one. Generous for real filings
    #: (most carry a handful) while bounding the pathological many-file disclosure.
    MAX_FILES_DEFAULT = 20

    async def detail(
        self, edge_no: str, *, max_files: int = MAX_FILES_DEFAULT
    ) -> Served[DisclosureDetail]:
        """A published disclosure never changes, so this is cached permanently.

        `max_files` caps the attachments *returned*; the memoised model keeps the full
        list, and the result always reports `attachments_total` (with
        `attachments_truncated` when the cap bit). Because the cache is immutable, a
        repeat call with a higher cap serves the rest without touching PSE Edge.
        """
        served = await self._cache.get(
            f"disclosure:{edge_no}",
            lambda: self._source.fetch_disclosure_viewer(edge_no),
            policy="immutable",
        )

        def build(html: str) -> DisclosureDetail:
            parsed = parse_disclosure_viewer(html)
            parsed["edge_no"] = parsed.get("edge_no") or edge_no
            parsed["body_html_url"] = self._absolute(parsed.get("body_html_url"))
            for attachment in parsed["attachments"]:
                attachment["download_url"] = self._absolute(attachment["download_url"])
            parsed.pop("body_file_id", None)
            parsed["attachments_total"] = len(parsed["attachments"])
            return DisclosureDetail(**parsed)

        full = self._memo.resolve(f"disclosure:{edge_no}#detail", served, build)
        return full.map(lambda detail: _cap_attachments(detail, max_files))

    def _absolute(self, path: str | None) -> str | None:
        """Parsers emit site-relative paths; callers outside this process need absolute."""
        return f"{self._base_url}{path}" if path else None


class CompanyInfoRepository:
    """Profile, financial highlights, and dividends/rights for one company."""

    def __init__(
        self,
        source: CompanyInfoSource,
        companies: CompanyRepository,
        cache: FrozenCache,
        memo: ParsedMemo | None = None,
    ) -> None:
        self._source = source
        self._companies = companies
        self._cache = cache
        self._memo = memo or ParsedMemo()

    async def profile(self, symbol: str) -> Served[CompanyProfile]:
        company = await self._companies.resolve(symbol)
        company_id = company.value.company_id
        key = f"company_info:{company_id}"
        served = await self._cache.get(
            key, lambda: self._source.fetch_company_information(company_id), policy="daily-refresh"
        )

        def build(html: str) -> CompanyProfile:
            parsed = parse_company_profile(html)
            parsed["company_id"] = company_id
            # The profile page's own header is thinner than autocomplete's JSON.
            parsed["company_name"] = parsed.get("company_name") or company.value.name
            return CompanyProfile(**parsed)

        return self._memo.resolve(f"{key}#profile", served, build)

    async def financials(self, symbol: str) -> Served[FinancialHighlights]:
        company = await self._companies.resolve(symbol)
        company_id = company.value.company_id
        key = f"financials:{company_id}"
        served = await self._cache.get(
            key, lambda: self._source.fetch_financial_reports(company_id), policy="daily-refresh"
        )

        def build(html: str) -> FinancialHighlights:
            parsed = parse_financial_reports(html)
            return FinancialHighlights(
                company_id=company_id,
                company_name=company.value.name,
                periods=[FinancialPeriod(**period) for period in parsed["periods"]],
                note=parsed["note"],
            )

        return self._memo.resolve(f"{key}#financials", served, build)

    async def dividends_and_rights(self, symbol: str) -> Served[DividendsAndRights]:
        """Two upstream calls: the page is a shell with a tab per kind.

        Both go through the freeze cache under their own keys, so a repeat request costs
        PSE Edge nothing, and the pair is combined into one result for the caller.
        """
        company = await self._companies.resolve(symbol)
        company_id = company.value.company_id

        dividends = await self._cache.get(
            f"dividends:{company_id}",
            lambda: self._source.fetch_dividends_or_rights(company_id, "Dividends"),
            policy="daily-refresh",
        )
        rights = await self._cache.get(
            f"rights:{company_id}",
            lambda: self._source.fetch_dividends_or_rights(company_id, "Rights"),
            policy="daily-refresh",
        )

        combined = DividendsAndRights(
            company_id=company_id,
            dividends=[DividendRecord(**r) for r in parse_dividends(dividends.value)],
            rights=[RightsRecord(**r) for r in parse_rights(rights.value)],
        )
        # Report the older of the two fetches, so freshness is never overstated.
        meta = dividends.meta if dividends.meta.as_of <= rights.meta.as_of else rights.meta
        return Served(value=combined, meta=meta)


class MarketRepository:
    """Market-wide indices and the homepage summary.

    Both come off the same server-rendered homepage, cached under one key, so asking for
    indices and then the summary costs a single upstream fetch.
    """

    HOMEPAGE_KEY = "homepage"

    def __init__(
        self, source: MarketSource, cache: FrozenCache, memo: ParsedMemo | None = None
    ) -> None:
        self._source = source
        self._cache = cache
        self._memo = memo or ParsedMemo()

    async def _homepage(self) -> Served[str]:
        # Fetched intraday on a miss like every non-price domain: index levels here are a
        # point-in-time snapshot, reused until the next close (see meta.as_of).
        return await self._cache.get(
            self.HOMEPAGE_KEY, lambda: self._source.fetch_homepage(), policy="daily-refresh"
        )

    async def indices(self) -> Served[MarketIndices]:
        served = await self._homepage()
        # `#indices` matters: summary() memoises a different shape from the same cached
        # homepage, and a shared memo key would hand one tool the other's result.
        return self._memo.resolve(
            f"{self.HOMEPAGE_KEY}#indices",
            served,
            lambda html: MarketIndices(indices=[IndexQuote(**row) for row in parse_indices(html)]),
        )

    async def summary(self) -> Served[MarketSummary]:
        served = await self._homepage()

        def build(html: str) -> MarketSummary:
            parsed = parse_market_summary(html)
            return MarketSummary(
                indices=[IndexQuote(**row) for row in parsed["indices"]],
                feeds={
                    label: [FeedItem(**item) for item in items]
                    for label, items in parsed["feeds"].items()
                },
            )

        return self._memo.resolve(f"{self.HOMEPAGE_KEY}#summary", served, build)
