"""MCP boundary: tool definitions, argument validation, and result shaping.

This module deliberately holds no domain logic. Each tool validates its arguments,
delegates to a repository, and shapes the reply. Deciding which PSE Edge endpoint
answers a question, building cache keys, parsing HTML, and constructing models all live
in `repositories.py`, so this file changes only when the *tool surface* changes.

Error mapping is applied once, by `reply()`, rather than repeated in every tool — it
was six copies of the same try/except before.

Tools (Phase 1): search_companies, get_stock_quote, get_price_history.
Tools (Phase 2): search_disclosures, search_disclosure_fulltext, get_disclosure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

try:  # MCP SDK >= 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # older SDKs shipped the same API as FastMCP
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef,import-not-found]

from pydantic import BaseModel

from .client import PseEdgeClient
from .config import Settings
from .errors import PseEdgeMcpError
from .market_calendar import MarketCalendar
from .repositories import CompanyRepository, DisclosureRepository, QuoteRepository
from .service import FreezeService, Served
from .validation import (
    optional_date,
    require_edge_no,
    require_ordered,
    require_page,
    require_text,
    resolve_window,
)

INSTRUCTIONS = """Data source: PSE Edge (Philippine Stock Exchange disclosure portal, unofficial).
This server serves END-OF-DAY data by design: values are frozen between market
sessions to avoid loading PSE Edge during trading hours (Asia/Manila). Every
result carries meta.as_of / meta.valid_until; meta.stale=true means the market
is open and you are seeing the latest end-of-day values. If you get a
MARKET_OPEN_NO_CACHE error, retry after the market closes (15:00 Manila)."""

# ~6 months, the default span for a price-history request.
DEFAULT_HISTORY_DAYS = 182
# Disclosure searches default to a recent window rather than all of history.
DEFAULT_DISCLOSURE_DAYS = 30


def _payload(served: Served[Any]) -> dict[str, Any]:
    """Shape the one response envelope every tool returns.

    Models are dumped in JSON mode so datetimes serialise; `meta` rides along so a
    client can always tell how fresh the answer is.
    """
    value = served.value
    if isinstance(value, BaseModel):
        data: Any = value.model_dump(mode="json")
    elif isinstance(value, list):
        data = [v.model_dump(mode="json") if isinstance(v, BaseModel) else v for v in value]
    else:
        data = value
    return {"data": data, "meta": served.meta.model_dump(mode="json")}


async def reply(call: Callable[[], Awaitable[Served[Any]]]) -> dict[str, Any]:
    """Run a tool body and shape its reply, mapping our errors to structured payloads.

    A `PseEdgeMcpError` must reach the client as a machine-readable
    `{"error": CODE, ...}` value rather than an exception, so an LLM can react to
    MARKET_OPEN_NO_CACHE or SYMBOL_NOT_FOUND instead of seeing a traceback. Doing it
    here means a new error code is handled for every tool at once, and a new tool cannot
    forget the mapping.

    Taking a callable rather than an awaitable is deliberate: argument validation runs
    *inside* the try, so an invalid argument returns INVALID_ARGUMENT like any other
    error instead of escaping as an unstructured tool failure.

    This is a helper and not a decorator because the MCP SDK derives each tool's output
    schema from its return annotation; a `functools.wraps` wrapper makes
    `inspect.signature` follow `__wrapped__` and pick up the inner annotation instead.
    """
    try:
        return _payload(await call())
    except PseEdgeMcpError as exc:
        return exc.payload()


def build_server(
    settings: Settings | None = None, calendar: MarketCalendar | None = None
) -> MCPServer:
    """`calendar` is injectable so tests can pin the freeze clock to a fixed moment."""
    settings = settings or Settings.from_env()
    calendar = calendar or MarketCalendar(
        tz=settings.market_tz, open_time=settings.market_open, close_time=settings.market_close
    )
    client = PseEdgeClient(settings)
    cache = FreezeService(calendar=calendar)

    companies = CompanyRepository(client, cache)
    quotes = QuoteRepository(client, companies, cache)
    disclosures = DisclosureRepository(client, cache, settings.base_url)

    mcp = MCPServer("pse-edge", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def search_companies(query: str) -> dict[str, Any]:
        """Search PSE-listed companies by name or ticker symbol.

        Returns matches with company_id, name, symbol. Use the symbol with the
        other tools.
        """
        return await reply(lambda: companies.search(require_text(query, "query")))

    @mcp.tool()
    async def get_stock_quote(symbol: str) -> dict[str, Any]:
        """Get the latest end-of-day quote for a PSE stock symbol (e.g. SM, AREIT, BDO).

        Includes price, change, 52-week range, market cap, shares, and the full
        set of fields PSE Edge publishes. Data is EOD-frozen (see meta).
        """
        return await reply(lambda: quotes.quote(require_text(symbol, "symbol")))

    @mcp.tool()
    async def get_price_history(
        symbol: str, start_date: str | None = None, end_date: str | None = None
    ) -> dict[str, Any]:
        """Get daily OHLC price history for a PSE stock symbol.

        Dates are ISO format (YYYY-MM-DD). Defaults to the last ~6 months.
        Data comes from PSE Edge's own chart endpoint and is EOD-frozen.
        """

        async def run() -> Served[Any]:
            start, end = resolve_window(start_date, end_date, default_days=DEFAULT_HISTORY_DAYS)
            return await quotes.history(require_text(symbol, "symbol"), start, end)

        return await reply(run)

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

        async def run() -> Served[Any]:
            require_page(page)
            company_id = None
            if symbol:
                resolved = await companies.resolve(symbol)
                company_id = resolved.value.company_id

            # A symbol with no dates means "everything this company ever filed", which
            # the per-company endpoint serves without a window; anything else needs one.
            window = None
            if not (company_id and start_date is None and end_date is None):
                window = resolve_window(start_date, end_date, default_days=DEFAULT_DISCLOSURE_DAYS)

            return await disclosures.search(
                company_id=company_id, window=window, template=template or "", page=page
            )

        return await reply(run)

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

        async def run() -> Served[Any]:
            require_page(page)
            term = require_text(keyword, "keyword")
            start = optional_date(start_date, "start_date")
            end = optional_date(end_date, "end_date")
            require_ordered(start, end)

            company_id = ""
            if symbol:
                resolved = await companies.resolve(symbol)
                company_id = resolved.value.company_id

            return await disclosures.fulltext(
                keyword=term,
                window=(start, end),
                company_id=company_id,
                subject_title=subject_title or "",
                page=page,
            )

        return await reply(run)

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
        return await reply(lambda: disclosures.detail(require_edge_no(edge_no)))

    return mcp
