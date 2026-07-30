"""Domain layer: one repository per data domain.

Each repository owns the full path from a domain question to a validated model —
cache key, freeze-policy read, HTML/JSON parse, model construction — for exactly one
domain. That placement is the point:

- **Single responsibility:** a cache key now lives beside the fetch it identifies.
  When these were built inline in the tool functions, a typo produced a silent cache
  miss or, worse, a collision between two different queries.
- **Open/closed:** a new data domain (Phase 3's financial reports, dividends, indices)
  is a new repository. Adding one does not touch existing repositories or the tools.
- **Dependency inversion:** repositories depend on the narrow protocols in `sources.py`
  and on `FrozenCache`, never on `PseEdgeClient` or `FreezeService` concretely, so they
  can be exercised with small fakes and no HTTP.

Repositories return `Served[Model]`, so freshness metadata travels with the data and
cannot be dropped on the way to the caller.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from .errors import InvalidArgumentError, SymbolNotFoundError
from .models import (
    CompanyHit,
    DisclosureDetail,
    DisclosureHit,
    DisclosureSearchResult,
    KeywordHit,
    KeywordSearchResult,
    OhlcBar,
    PriceHistory,
    StockQuote,
)
from .parsers import (
    parse_chart_date,
    parse_disclosure_table,
    parse_disclosure_viewer,
    parse_keyword_results,
    parse_stock_data_page,
)
from .service import FrozenCache, Served
from .sources import CompanySource, DisclosureSource, QuoteSource

SearchSource = Literal["announcements", "company_disclosures"]


def _company_hit(raw: dict[str, Any]) -> CompanyHit:
    """Autocomplete's wire shape -> our model. One place, so search and resolve agree."""
    return CompanyHit(
        company_id=raw["cmpyId"],
        name=raw.get("cmpyNm", ""),
        symbol=raw["symbol"],
        is_etf=raw.get("etfYn") == "1",
    )


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
        wanted = symbol.strip().upper()
        served = await self._raw(wanted)
        for hit in served.value:
            if hit.get("symbol", "").upper() == wanted:
                # Built eagerly rather than through `.map`, so nothing closes over the
                # loop variable.
                return Served(value=_company_hit(hit), meta=served.meta)
        raise SymbolNotFoundError(f"No PSE-listed company found for symbol '{wanted}'")

    async def _raw(self, query: str) -> Served[list[dict[str, Any]]]:
        normalised = query.strip().upper()
        return await self._cache.get(
            f"autocomplete:{normalised}",
            lambda: self._source.search_companies(query.strip()),
        )


class QuoteRepository:
    """Quotes and OHLC history. Both need a security_id, which only the quote page has."""

    def __init__(
        self, source: QuoteSource, companies: CompanyRepository, cache: FrozenCache
    ) -> None:
        self._source = source
        self._companies = companies
        self._cache = cache

    async def quote(self, symbol: str) -> Served[StockQuote]:
        parsed = await self._quote_page(symbol)
        return parsed.map(lambda fields: StockQuote(**fields))

    async def history(self, symbol: str, start: date, end: date) -> Served[PriceHistory]:
        parsed = await self._quote_page(symbol)
        company_id = parsed.value["company_id"]
        security_id = parsed.value["security_id"]

        served = await self._cache.get(
            f"ohlc:{company_id}:{security_id}:{start.isoformat()}:{end.isoformat()}",
            lambda: self._source.fetch_price_history(company_id, security_id, start, end),
        )
        return served.map(
            lambda data: PriceHistory(
                symbol=parsed.value["symbol"],
                company_id=company_id,
                security_id=security_id,
                start_date=start,
                end_date=end,
                bars=[
                    OhlcBar(
                        trade_date=parse_chart_date(row["CHART_DATE"]),
                        open=row["OPEN"],
                        high=row["HIGH"],
                        low=row["LOW"],
                        close=row["CLOSE"],
                        value=row["VALUE"],
                    )
                    for row in data["chartData"]
                ],
            )
        )

    async def _quote_page(self, symbol: str) -> Served[dict[str, Any]]:
        """Fetch and parse the quote page, backfilling identity from autocomplete.

        The page's own header is the weakest part of the parse, so name/symbol fall back
        to the autocomplete result, which is clean JSON.
        """
        company = await self._companies.resolve(symbol)
        company_id = company.value.company_id
        served = await self._cache.get(
            f"stock_data:{company_id}",
            lambda: self._source.fetch_stock_data_page(company_id),
        )
        return served.map(lambda html: _quote_fields(html, company.value))


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

    def __init__(self, source: DisclosureSource, cache: FrozenCache, base_url: str) -> None:
        self._source = source
        self._cache = cache
        self._base_url = base_url.rstrip("/")

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
            served = await self._cache.get(
                f"company_disclosures:{company_id}:{template}:{page}",
                lambda: self._source.search_company_disclosures(
                    company_id, template=template, page=page
                ),
            )
            source_name = "company_disclosures"
        elif window is not None:
            start, end = window
            served = await self._cache.get(
                f"announcements:{company_id or ''}:{start.isoformat()}:"
                f"{end.isoformat()}:{template}:{page}",
                lambda: self._source.search_announcements(
                    from_date=start,
                    to_date=end,
                    company_id=company_id or "",
                    template=template,
                    page=page,
                ),
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

        return served.map(build)

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
        served = await self._cache.get(
            f"keyword:{keyword}:{start}:{end}:{company_id}:{subject_title}:{page}",
            lambda: self._source.search_disclosure_fulltext(
                keyword,
                from_date=start,
                to_date=end,
                company_id=company_id,
                subject_title=subject_title,
                page=page,
            ),
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

        return served.map(build)

    async def detail(self, edge_no: str) -> Served[DisclosureDetail]:
        """A published disclosure never changes, so this is cached permanently."""
        served = await self._cache.get(
            f"disclosure:{edge_no}",
            lambda: self._source.fetch_disclosure_viewer(edge_no),
            immutable=True,
        )

        def build(html: str) -> DisclosureDetail:
            parsed = parse_disclosure_viewer(html)
            parsed["edge_no"] = parsed.get("edge_no") or edge_no
            parsed["body_html_url"] = self._absolute(parsed.get("body_html_url"))
            for attachment in parsed["attachments"]:
                attachment["download_url"] = self._absolute(attachment["download_url"])
            parsed.pop("body_file_id", None)
            return DisclosureDetail(**parsed)

        return served.map(build)

    def _absolute(self, path: str | None) -> str | None:
        """Parsers emit site-relative paths; callers outside this process need absolute."""
        return f"{self._base_url}{path}" if path else None
