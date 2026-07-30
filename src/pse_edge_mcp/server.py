"""FastMCP server wiring.

Phase 1: search_companies, get_stock_quote, get_price_history.
Phase 2: search_disclosures, search_disclosure_fulltext, get_disclosure.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

try:  # MCP SDK >= 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # older SDKs shipped the same API as FastMCP
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef,import-not-found]

from .client import PseEdgeClient
from .config import Settings
from .errors import InvalidArgumentError, PseEdgeMcpError, SymbolNotFoundError
from .market_calendar import MarketCalendar
from .models import (
    CompanyHit,
    DisclosureDetail,
    DisclosureHit,
    DisclosureSearchResult,
    KeywordHit,
    KeywordSearchResult,
    Meta,
    OhlcBar,
    PriceHistory,
    StockQuote,
)
from .parsers import (
    EDGE_NO_RE,
    parse_disclosure_table,
    parse_disclosure_viewer,
    parse_keyword_results,
    parse_stock_data_page,
)
from .service import FreezeService

INSTRUCTIONS = """Data source: PSE Edge (Philippine Stock Exchange disclosure portal, unofficial).
This server serves END-OF-DAY data by design: values are frozen between market
sessions to avoid loading PSE Edge during trading hours (Asia/Manila). Every
result carries meta.as_of / meta.valid_until; meta.stale=true means the market
is open and you are seeing the latest end-of-day values. If you get a
MARKET_OPEN_NO_CACHE error, retry after the market closes (15:00 Manila)."""


def build_server(
    settings: Settings | None = None, calendar: MarketCalendar | None = None
) -> MCPServer:
    """`calendar` is injectable so tests can pin the freeze clock to a fixed moment."""
    settings = settings or Settings.from_env()
    calendar = calendar or MarketCalendar(
        tz=settings.market_tz, open_time=settings.market_open, close_time=settings.market_close
    )
    client = PseEdgeClient(settings)
    service = FreezeService(calendar=calendar)

    mcp = MCPServer("pse-edge", instructions=INSTRUCTIONS)

    def _result(payload: Any, meta: Meta) -> dict[str, Any]:
        return {"data": payload, "meta": meta.model_dump(mode="json")}

    async def _company_id(symbol: str) -> tuple[str, str]:
        """Symbol -> (company_id, company_name) via autocomplete only — one cheap hit.

        get_stock_quote needs the heavier stockData page for security_id; disclosure
        tools do not, so they stop here.
        """
        sym = symbol.strip().upper()
        served = await service.get(f"autocomplete:{sym}", lambda: client.search_companies(sym))
        exact = [h for h in served.value if h.get("symbol", "").upper() == sym]
        if not exact:
            raise SymbolNotFoundError(f"No PSE-listed company found for symbol '{sym}'")
        return exact[0]["cmpyId"], exact[0].get("cmpyNm", "")

    def _absolute(url: str | None) -> str | None:
        return f"{settings.base_url.rstrip('/')}{url}" if url else None

    async def _resolve(symbol: str) -> dict[str, Any]:
        """Symbol -> parsed stockData page fields (company_id, security_id, quote...)."""
        sym = symbol.strip().upper()
        served = await service.get(f"autocomplete:{sym}", lambda: client.search_companies(sym))
        exact = [h for h in served.value if h.get("symbol", "").upper() == sym]
        if not exact:
            raise SymbolNotFoundError(f"No PSE-listed company found for symbol '{sym}'")
        company_id = exact[0]["cmpyId"]
        quote_served = await service.get(
            f"stock_data:{company_id}",
            lambda: client.fetch_stock_data_page(company_id),
        )
        parsed = parse_stock_data_page(quote_served.value)
        if not parsed.get("symbol"):
            parsed["symbol"] = sym
        if not parsed.get("company_name"):
            parsed["company_name"] = exact[0].get("cmpyNm", "")
        parsed["_meta"] = quote_served.meta
        return parsed

    @mcp.tool()
    async def search_companies(query: str) -> dict[str, Any]:
        """Search PSE-listed companies by name or ticker symbol.

        Returns matches with company_id, name, symbol. Use the symbol with the
        other tools.
        """
        try:
            served = await service.get(
                f"autocomplete:{query.strip().upper()}",
                lambda: client.search_companies(query.strip()),
            )
            hits = [
                CompanyHit(
                    company_id=h["cmpyId"],
                    name=h["cmpyNm"],
                    symbol=h["symbol"],
                    is_etf=h.get("etfYn") == "1",
                ).model_dump()
                for h in served.value
            ]
            return _result(hits, served.meta)
        except PseEdgeMcpError as exc:
            return exc.payload()

    @mcp.tool()
    async def get_stock_quote(symbol: str) -> dict[str, Any]:
        """Get the latest end-of-day quote for a PSE stock symbol (e.g. SM, AREIT, BDO).

        Includes price, change, 52-week range, market cap, shares, and the full
        set of fields PSE Edge publishes. Data is EOD-frozen (see meta).
        """
        try:
            parsed = await _resolve(symbol)
            meta: Meta = parsed.pop("_meta")
            quote = StockQuote(**parsed)
            return _result(quote.model_dump(mode="json"), meta)
        except PseEdgeMcpError as exc:
            return exc.payload()

    @mcp.tool()
    async def get_price_history(
        symbol: str, start_date: str | None = None, end_date: str | None = None
    ) -> dict[str, Any]:
        """Get daily OHLC price history for a PSE stock symbol.

        Dates are ISO format (YYYY-MM-DD). Defaults to the last ~6 months.
        Data comes from PSE Edge's own chart endpoint and is EOD-frozen.
        """
        try:
            end = date.fromisoformat(end_date) if end_date else date.today()
            start = date.fromisoformat(start_date) if start_date else end - timedelta(days=182)
            parsed = await _resolve(symbol)
            company_id, security_id = parsed["company_id"], parsed["security_id"]

            served = await service.get(
                f"ohlc:{company_id}:{security_id}:{start.isoformat()}:{end.isoformat()}",
                lambda: client.fetch_price_history(company_id, security_id, start, end),
            )
            bars = [
                OhlcBar(
                    trade_date=PseEdgeClient.parse_chart_date(row["CHART_DATE"]),
                    open=row["OPEN"],
                    high=row["HIGH"],
                    low=row["LOW"],
                    close=row["CLOSE"],
                    value=row["VALUE"],
                )
                for row in served.value["chartData"]
            ]
            history = PriceHistory(
                symbol=parsed["symbol"],
                company_id=company_id,
                security_id=security_id,
                start_date=start,
                end_date=end,
                bars=bars,
            )
            return _result(history.model_dump(mode="json"), served.meta)
        except PseEdgeMcpError as exc:
            return exc.payload()

    @mcp.tool()
    async def search_disclosures(
        symbol: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        template: str | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """Search PSE company disclosures (announcements, material information, notices).

        Returns disclosure metadata — pass a hit's edge_no to get_disclosure for
        attachment links. 50 results per page; the result reports total/pages/has_more
        so you can request the next page directly.

        - symbol only: that company's full disclosure history, newest first.
        - date range (ISO YYYY-MM-DD): all companies' disclosures in that window,
          or one company's if symbol is also given. Defaults to the last 30 days
          when neither symbol nor dates are supplied.
        - template: filter by disclosure type as free text, e.g. "Press Release",
          "Cash Dividend", "Material Information".

        Data is EOD-frozen: a disclosure filed during today's session appears after
        the 15:00 Manila close (see meta).
        """
        try:
            if page < 1:
                raise InvalidArgumentError(f"page must be 1 or greater, got {page}")
            company_id = None
            if symbol:
                company_id, _ = await _company_id(symbol)

            tmpl = template or ""
            # No date window and a known company: companyDisclosures serves the whole
            # history in one query, which announcements (date-ranged) cannot do.
            if company_id and not start_date and not end_date:
                key = f"company_disclosures:{company_id}:{tmpl}:{page}"
                served = await service.get(
                    key,
                    lambda: client.search_company_disclosures(company_id, template=tmpl, page=page),
                )
                source: Literal["announcements", "company_disclosures"] = "company_disclosures"
            else:
                end = date.fromisoformat(end_date) if end_date else date.today()
                start = date.fromisoformat(start_date) if start_date else end - timedelta(days=30)
                if start > end:
                    raise InvalidArgumentError(
                        f"start_date {start.isoformat()} is after end_date {end.isoformat()}"
                    )
                key = (
                    f"announcements:{company_id or ''}:{start.isoformat()}:"
                    f"{end.isoformat()}:{tmpl}:{page}"
                )
                served = await service.get(
                    key,
                    lambda: client.search_announcements(
                        from_date=start,
                        to_date=end,
                        company_id=company_id or "",
                        template=tmpl,
                        page=page,
                    ),
                )
                source = "announcements"

            parsed = parse_disclosure_table(served.value)
            pages = parsed.get("pages")
            result = DisclosureSearchResult(
                hits=[DisclosureHit(**row) for row in parsed["rows"]],
                page=parsed.get("page") or page,
                pages=pages,
                total=parsed.get("total"),
                has_more=bool(pages and (parsed.get("page") or page) < pages),
                source=source,
            )
            return _result(result.model_dump(mode="json"), served.meta)
        except ValueError as exc:  # bad ISO date from the caller
            return InvalidArgumentError(f"invalid date: {exc}").payload()
        except PseEdgeMcpError as exc:
            return exc.payload()

    @mcp.tool()
    async def search_disclosure_fulltext(
        keyword: str,
        start_date: str | None = None,
        end_date: str | None = None,
        symbol: str | None = None,
        subject_title: str | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """Full-text search *inside* disclosure attachments, returning matching snippets.

        Use this to find wording within filings ("share buyback", "force majeure").
        For "what did company X disclose recently", use search_disclosures instead.

        IMPORTANT: PSE Edge's own full-text index is partial and lags behind — at last
        verification it covered roughly 2023-2025 and held nothing from 2026. Results
        are relevance-ordered (not chronological), 10 per page. The result includes a
        coverage_note; relay that limitation rather than reporting "no disclosures exist".
        """
        try:
            if page < 1:
                raise InvalidArgumentError(f"page must be 1 or greater, got {page}")
            if not keyword.strip():
                raise InvalidArgumentError("keyword must not be empty")
            company_id = ""
            if symbol:
                company_id, _ = await _company_id(symbol)
            start = date.fromisoformat(start_date) if start_date else None
            end = date.fromisoformat(end_date) if end_date else None
            if start and end and start > end:
                raise InvalidArgumentError(
                    f"start_date {start.isoformat()} is after end_date {end.isoformat()}"
                )
            subject = subject_title or ""

            served = await service.get(
                f"keyword:{keyword.strip()}:{start}:{end}:{company_id}:{subject}:{page}",
                lambda: client.search_disclosure_fulltext(
                    keyword.strip(),
                    from_date=start,
                    to_date=end,
                    company_id=company_id,
                    subject_title=subject,
                    page=page,
                ),
            )
            parsed = parse_keyword_results(served.value)
            pages = parsed.get("pages")
            result = KeywordSearchResult(
                hits=[KeywordHit(**hit) for hit in parsed["hits"]],
                page=parsed.get("page") or page,
                pages=pages,
                total=parsed.get("total"),
                has_more=bool(pages and (parsed.get("page") or page) < pages),
            )
            return _result(result.model_dump(mode="json"), served.meta)
        except ValueError as exc:
            return InvalidArgumentError(f"invalid date: {exc}").payload()
        except PseEdgeMcpError as exc:
            return exc.payload()

    @mcp.tool()
    async def get_disclosure(edge_no: str) -> dict[str, Any]:
        """Get one disclosure's details and attachment links by its edge_no.

        edge_no is the 32-character hex id returned by search_disclosures.
        Returns the company, template, date, related documents, and URLs for each
        attachment plus the rendered body HTML. This server does not download or parse
        attachments — fetch the returned URLs yourself if you need their contents.

        A published disclosure never changes, so these results are cached permanently
        (meta.data_policy is "immutable").
        """
        try:
            key = edge_no.strip().lower()
            if not EDGE_NO_RE.match(key):
                raise InvalidArgumentError(
                    "edge_no must be a 32-character hex id from "
                    f"search_disclosures, got '{edge_no}'"
                )
            served = await service.get(
                f"disclosure:{key}",
                lambda: client.fetch_disclosure_viewer(key),
                immutable=True,
            )
            parsed = parse_disclosure_viewer(served.value)
            parsed["edge_no"] = parsed.get("edge_no") or key
            parsed["body_html_url"] = _absolute(parsed.get("body_html_url"))
            for att in parsed["attachments"]:
                att["download_url"] = _absolute(att["download_url"])
            parsed.pop("body_file_id", None)
            detail = DisclosureDetail(**parsed)
            return _result(detail.model_dump(mode="json"), served.meta)
        except PseEdgeMcpError as exc:
            return exc.payload()

    return mcp
