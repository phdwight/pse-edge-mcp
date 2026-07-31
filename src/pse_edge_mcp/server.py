"""MCP boundary: tool definitions, argument validation, and result shaping.

This module deliberately holds no domain logic. Each tool validates its arguments,
delegates to a repository, and shapes the reply. Deciding which PSE Edge endpoint
answers a question, building cache keys, parsing HTML, and constructing models all live
in `repositories.py`, so this file changes only when the *tool surface* changes.

Error mapping is applied once, by `reply()`, rather than repeated in every tool — it
was six copies of the same try/except before.

Tools (Phase 1): search_companies, get_stock_quote, get_price_history.
Tools (Phase 2): search_disclosures, search_disclosure_fulltext, get_disclosure.
Tools (Phase 3): get_company_profile, get_financial_highlights, get_dividends_and_rights,
                 get_indices, get_market_summary.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

try:  # MCP SDK >= 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # older SDKs shipped the same API as FastMCP
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef,import-not-found]

from pydantic import BaseModel

from .archive import Archive, NullArchive
from .cache import Storage
from .client import PseEdgeClient
from .config import Settings
from .errors import PseEdgeMcpError
from .market_calendar import MarketCalendar
from .memo import ParsedMemo
from .repositories import (
    CompanyInfoRepository,
    CompanyRepository,
    DisclosureRepository,
    MarketRepository,
    QuoteRepository,
)
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


def build_storage(settings: Settings) -> tuple[Storage | None, Archive, AsyncEngine | None]:
    """Pick the storage backend from configuration.

    `DATABASE_URL` unset is the zero-config stdio path: an in-memory cache and no archive.
    Set, it selects Postgres, which buys two things (plan §5): replicas share one cache, so
    the freeze policy still means one upstream fetch per boundary no matter how many
    processes run; and reads accumulate into the opportunistic archive.

    Returning `None` for storage leaves `FreezeService` to construct its own default, so
    there is exactly one place that decides what "no database" means.
    """
    if not settings.database_url:
        return None, NullArchive(), None

    # Imported here, not at module scope: these pull in SQLAlchemy and asyncpg, which a
    # plain install without the `postgres` extra does not have. A stdio user must never
    # hit an ImportError for a backend they did not ask for.
    from .archive_postgres import PostgresArchive
    from .db import create_engine, normalise_url
    from .storage_postgres import PostgresStorage

    engine = create_engine(
        normalise_url(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    # The engine rides along so callers that need another consumer of the same pool
    # (auth's token lookups) don't open a second one to the same database.
    return PostgresStorage(engine), PostgresArchive(engine), engine


def build_server(
    settings: Settings | None = None,
    calendar: MarketCalendar | None = None,
    storage: Storage | None = None,
    archive: Archive | None = None,
) -> MCPServer:
    """`calendar`, `storage` and `archive` are injectable so tests can pin the freeze clock
    and swap the backend without a database."""
    settings = settings or Settings.from_env()
    calendar = calendar or MarketCalendar(
        tz=settings.market_tz, open_time=settings.market_open, close_time=settings.market_close
    )
    if storage is None and archive is None:
        storage, archive, _engine = build_storage(settings)
    client = PseEdgeClient(settings)
    cache = FreezeService(calendar=calendar, storage=storage)

    # One memo shared by every repository: parsing is the dominant per-request cost and
    # its result cannot change before the freeze boundary, so reusing it is free
    # correctness-wise. See memo.py.
    memo = ParsedMemo()

    companies = CompanyRepository(client, cache)
    quotes = QuoteRepository(client, companies, cache, archive, memo)
    disclosures = DisclosureRepository(client, cache, settings.base_url, archive, memo)
    company_info = CompanyInfoRepository(client, companies, cache, memo)
    market = MarketRepository(client, cache, memo)

    # `version` is not optional in spirit: it goes into serverInfo on every initialize, and
    # leaving it unset advertised an empty string to every client. Read from the installed
    # distribution so it cannot drift from pyproject.toml.
    try:
        _version = importlib.metadata.version("pse-edge-mcp")
    except importlib.metadata.PackageNotFoundError:  # running from a source tree
        _version = "0.0.0+unknown"
    mcp = MCPServer("pse-edge", version=_version, instructions=INSTRUCTIONS)

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

    # ---- Phase 3: company info & market ------------------------------------

    @mcp.tool()
    async def get_company_profile(symbol: str) -> dict[str, Any]:
        """Get a PSE-listed company's profile: sector, incorporation, auditor, contacts.

        Includes sector and subsector, incorporation date, corporate life, number of
        directors, fiscal year end, stockholders' meeting schedule, external auditor,
        transfer agent, business address, phone, fax, email and website. Every label on
        the page is also returned verbatim in raw_fields.
        """
        return await reply(lambda: company_info.profile(require_text(symbol, "symbol")))

    @mcp.tool()
    async def get_financial_highlights(symbol: str) -> dict[str, Any]:
        """Get the financial highlights PSE Edge publishes for a company.

        Returns an annual and a quarterly section, each with a balance sheet and an income
        statement, using Edge's own line-item labels and column headings (annual compares
        Current/Previous Year; quarterly compares Period Ended against the audited fiscal
        year, and its income statement has four columns including year-to-date).

        IMPORTANT — units: figures are returned exactly as Edge prints them and are never
        rescaled. Each period carries its own `currency_units` label, and the two sections
        disagree in practice (observed: annual "Php (in thousands)" while quarterly said
        "Php (in Millions)" for the same company, with the same figure appearing in both).
        Read `currency_units` before quoting any number, and say the scale is uncertain
        rather than presenting these as exact peso amounts. Only the highlights Edge
        serves as data are here — this server does not parse filed PDF statements.
        """
        return await reply(lambda: company_info.financials(require_text(symbol, "symbol")))

    @mcp.tool()
    async def get_dividends_and_rights(symbol: str) -> dict[str, Any]:
        """Get a company's declared dividends and stock rights offers.

        Dividends carry the security and dividend type, the rate as printed, and the
        ex-dividend, record and payment dates. Rights carry the entitlement ratio, offer
        price, ex-rights date and offer period. Each record includes the `edge_no` of the
        disclosure that announced it, so you can pass it to get_disclosure for the notice
        itself. Empty lists mean Edge lists none for this company.
        """
        return await reply(
            lambda: company_info.dividends_and_rights(require_text(symbol, "symbol"))
        )

    @mcp.tool()
    async def get_indices() -> dict[str, Any]:
        """Get PSEi and the PSE sector index levels with their daily change.

        Covers PSEi, All Shares, Financials, Industrial, Holding Firms, Property, Services
        and Mining and Oil. `change` and `change_percent` are signed, and `direction` is
        "up"/"down"/"flat" — PSE Edge prints these unsigned and shows direction only as a
        colour and an arrow, so the signs here are derived from that. EOD-frozen: during a
        session you get the previous close's figures (see meta.stale).
        """
        return await reply(market.indices)

    @mcp.tool()
    async def get_market_summary() -> dict[str, Any]:
        """Get a market-wide snapshot: index levels plus PSE Edge's homepage feeds.

        `feeds` is keyed by Edge's own group labels — Company Announcements, Financial
        Reports, Other Reports, Listing Notices, Disclosure Notices, and the most-viewed
        disclosures for Today and This Week. Each entry carries its symbol, timestamp,
        circular number and `edge_no` for get_disclosure.

        Note: PSE Edge publishes no gainers/losers/most-active data anywhere, so this
        cannot include them — say so rather than implying the data is missing or stale.
        """
        return await reply(market.summary)

    return mcp
