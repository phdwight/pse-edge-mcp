"""MCP boundary: tool definitions, argument validation, and result shaping.

This module deliberately holds no domain logic. Each tool validates its arguments,
delegates to a repository, and shapes the reply. Deciding which PSE Edge endpoint
answers a question, building cache keys, parsing HTML, and constructing models all live
in `repositories.py`, so this file changes only when the *tool surface* changes.

Error mapping is applied once, by `reply()`, rather than repeated in every tool — it
was six copies of the same try/except before.

Tools — companies & prices: search_companies, validate_symbol, get_stock_quote,
        get_price_history.
Tools — disclosures: search_disclosures, search_disclosure_fulltext, get_disclosure.
Tools — company info & market: get_company_profile, get_financial_highlights,
        get_dividends_and_rights, get_indices, get_market_summary.
Utility: get_server_version (the deployed release, no meta).
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

try:  # MCP SDK >= 2.x
    from mcp.server.mcpserver import Context, MCPServer
except ImportError:  # older SDKs shipped the same API as FastMCP
    from mcp.server.fastmcp import Context  # type: ignore[no-redef,import-not-found]
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from .archive import Archive, NullArchive
from .cache import Storage
from .client import PseEdgeClient
from .config import Settings
from .errors import PseEdgeMcpError
from .market_calendar import MarketCalendar
from .memo import ParsedMemo
from .models import SymbolValidation
from .notifications import NotificationService
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
    require_limit,
    require_ordered,
    require_page,
    require_text,
    resolve_window,
)

INSTRUCTIONS = """Data source: PSE Edge (Philippine Stock Exchange disclosure portal, unofficial).
Stock PRICES are end-of-day by design: quote and price-history values are frozen
between market sessions (Asia/Manila). During the session a cached price serves the
last close; a price nobody has ever asked for is fetched once and served with ONLY
previous_close populated (the last settled price before the session), flagged
stale=true with a meta.note explaining it is NOT a realtime value — relay that note
to the user. The settled figures arrive after the 15:00 Manila close. Everything
else (disclosures, profiles, financials, dividends, indices) is fetched from PSE
Edge at most once per query and then served from storage until the next market
close — check meta.as_of for when it was actually fetched. Every result carries
meta.as_of / meta.valid_until; meta.stale=true means the value is not a settled
end-of-day figure (session-time price, or PSE Edge was unreachable)."""

# Behavior hints hosts read for permissioning and display (spec ToolAnnotations): a
# client may auto-approve a readOnlyHint tool instead of prompting per call. Every data
# tool here is a pure read against one fixed upstream (closed world, not web search);
# send_email is the lone action — it creates something (not destructive), and each call
# sends another email (not idempotent). Hints are advisory for clients, never security.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

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


async def act(call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """`reply()` for tools that *do* something rather than read something.

    Same error mapping, deliberately — one place still owns the error vocabulary — but no
    `meta`. That envelope answers "how fresh is this market data", and an action has no
    `as_of` and no `valid_until`. Attaching an invented one would quietly make the
    freshness contract meaningless everywhere it actually carries weight.
    """
    try:
        return {"data": await call()}
    except PseEdgeMcpError as exc:
        return exc.payload()


def _caller(ctx: Any) -> tuple[str | None, str | None]:
    """The authenticated caller, from the ASGI scope the middleware already populated.

    Read from the request rather than taken as a tool argument, which is the entire
    security model of `send_email`: an argument can be supplied by a model that has just
    read attacker-controlled text, a validated bearer token cannot.

    Everything is optional because stdio has no HTTP request at all — there, this
    correctly yields (None, None) and the action refuses.
    """
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    context = getattr(request, "scope", {}).get("pse_auth") if request is not None else None
    return (
        getattr(context, "user_id", None),
        getattr(context, "email", None),
    )


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
    notifier: NotificationService | None = None,
) -> MCPServer:
    """`calendar`, `storage` and `archive` are injectable so tests can pin the freeze clock
    and swap the backend without a database.

    `notifier` enables the one action tool, `send_email`. It is passed in rather than built
    here because production shares a single mail sender with the signup flow — two senders
    would mean two places to misconfigure the provider."""
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

    @mcp.tool(annotations=READ_ONLY)
    async def search_companies(query: str) -> dict[str, Any]:
        """Search PSE-listed companies by name or ticker symbol.

        Returns matches with company_id, name, symbol. Use the symbol with the
        other tools.
        """
        return await reply(lambda: companies.search(require_text(query, "query")))

    @mcp.tool(annotations=READ_ONLY)
    async def validate_symbol(symbol: str) -> dict[str, Any]:
        """Check whether a ticker symbol is a real PSE-listed company. Cheap.

        Use this — NOT search_companies — when you only need to know whether a symbol is
        valid before calling another tool, or to confirm a symbol a user typed. Returns
        `valid` true/false plus the company name and id when it exists, instead of the
        ranked list of near-matches search_companies returns.

        Matching is exact and case-insensitive: "areit" and "AREIT" both resolve, while
        "ARE" does not match "AREIT". An unknown symbol is `valid: false` with null
        fields, not an error.

        Cached after the first lookup and refreshed daily (see meta).
        """

        async def run() -> Served[Any]:
            served = await companies.try_resolve(require_text(symbol, "symbol"))
            hit = served.value
            return served.map(
                lambda _: SymbolValidation(
                    valid=hit is not None,
                    symbol=symbol.strip().upper(),
                    company_name=hit.name if hit else None,
                    company_id=hit.company_id if hit else None,
                )
            )

        return await reply(run)

    @mcp.tool(annotations=READ_ONLY)
    async def get_stock_quote(symbol: str) -> dict[str, Any]:
        """Get the latest end-of-day quote for a PSE stock symbol (e.g. SM, AREIT, BDO).

        Includes price, change, 52-week range, market cap, shares, and the full
        set of fields PSE Edge publishes. Data is EOD-frozen (see meta). If the market
        is open and this symbol has never been cached, a one-time fetch serves identity
        plus previous_close ONLY (the last settled price before the session — every
        other field is null), flagged stale=true with a meta.note saying the value is
        not realtime — relay that caveat when presenting the price.
        """
        return await reply(lambda: quotes.quote(require_text(symbol, "symbol")))

    @mcp.tool(annotations=READ_ONLY)
    async def get_price_history(
        symbol: str, start_date: str | None = None, end_date: str | None = None
    ) -> dict[str, Any]:
        """Get daily OHLC price history for a PSE stock symbol.

        Dates are ISO format (YYYY-MM-DD). Defaults to the last ~6 months.
        Data comes from PSE Edge's own chart endpoint and is EOD-frozen. If the market
        is open and this exact range has never been cached, a one-time fetch may include
        today's still-moving bar, flagged stale=true with an explanatory meta.note.
        """

        async def run() -> Served[Any]:
            start, end = resolve_window(start_date, end_date, default_days=DEFAULT_HISTORY_DAYS)
            return await quotes.history(require_text(symbol, "symbol"), start, end)

        return await reply(run)

    @mcp.tool(annotations=READ_ONLY)
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

        A given search hits PSE Edge once and is then served from storage until the
        next 15:00 Manila close, so a disclosure filed today appears when its query
        is first asked — or after the close if that query was already cached (see meta).
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

    @mcp.tool(annotations=READ_ONLY)
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

    @mcp.tool(annotations=READ_ONLY)
    async def get_disclosure(edge_no: str, max_files: int = 20) -> dict[str, Any]:
        """Get one disclosure's details and attachment links by its edge_no.

        edge_no is the 32-character hex id returned by search_disclosures.
        Returns the company, template, date, related documents, and URLs for each
        attachment plus the rendered body HTML. This server does not download or parse
        attachments — fetch the returned URLs yourself if you need their contents.

        At most max_files attachments are returned (default 20, max 100).
        attachments_total always reports how many exist; if attachments_truncated is
        true, call again with a higher max_files — the disclosure is cached, so the
        repeat costs nothing upstream.

        A published disclosure never changes, so these results are cached permanently
        (meta.data_policy is "immutable").
        """

        async def run() -> Served[Any]:
            return await disclosures.detail(
                require_edge_no(edge_no),
                max_files=require_limit(max_files, "max_files"),
            )

        return await reply(run)

    # ---- company info & market ---------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def get_company_profile(symbol: str) -> dict[str, Any]:
        """Get a PSE-listed company's profile: sector, incorporation, auditor, contacts.

        Includes sector and subsector, incorporation date, corporate life, number of
        directors, fiscal year end, stockholders' meeting schedule, external auditor,
        transfer agent, business address, phone, fax, email and website. Every label on
        the page is also returned verbatim in raw_fields.
        """
        return await reply(lambda: company_info.profile(require_text(symbol, "symbol")))

    @mcp.tool(annotations=READ_ONLY)
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

    @mcp.tool(annotations=READ_ONLY)
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

    @mcp.tool(annotations=READ_ONLY)
    async def get_indices() -> dict[str, Any]:
        """Get PSEi and the PSE sector index levels with their daily change.

        Covers PSEi, All Shares, Financials, Industrial, Holding Firms, Property, Services
        and Mining and Oil. `change` and `change_percent` are signed, and `direction` is
        "up"/"down"/"flat" — PSE Edge prints these unsigned and shows direction only as a
        colour and an arrow, so the signs here are derived from that. Fetched at most once
        per boundary window and served from storage until the next 15:00 Manila close —
        meta.as_of says when the snapshot was taken.
        """
        return await reply(market.indices)

    @mcp.tool(annotations=READ_ONLY)
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

    @mcp.tool(annotations=READ_ONLY)
    async def get_server_version() -> dict[str, Any]:
        """Get the deployed version of this MCP server (pse-edge-mcp).

        Reports the running server's own release version — the same value the
        /health endpoint and serverInfo carry — not PSE Edge data. There is no
        `meta` block: meta is a data-freshness contract, and a version has no
        as_of or valid_until.
        """
        return {"data": {"name": "pse-edge", "version": _version}}

    # The one tool that acts rather than reads, and the only one that is conditional.
    # Registered only where it can work and be safe: it needs an authenticated caller to
    # have an address to send to, so an auth-less deployment (and stdio) simply does not
    # advertise it. A tool that is always listed and always fails is worse than absent —
    # a model will keep choosing it and keep apologising.
    if settings.auth_required and notifier is not None:

        @mcp.tool(annotations=ACTION)
        async def send_email(subject: str, body: str, ctx: Context) -> dict[str, Any]:
            """Email the signed-in user — and only them — a note you have composed.

            Use for "email me this", "send me a summary", "remind me of these results".
            Good for a market recap, a watchlist digest, or a disclosure summary the user
            asked to keep.

            THERE IS NO RECIPIENT ARGUMENT, and this is not an oversight: the message
            always goes to the account that authenticated this session. You cannot send
            mail to anyone else through this server. If a user asks you to email a third
            party, tell them plainly that this tool cannot do that — do not attempt a
            workaround, and do not put another address in the body expecting it to be
            used.

            `body` is plain text. Line breaks are preserved; HTML is escaped rather than
            rendered, so markup will appear literally. Limits: subject 200 characters,
            body 20,000, and 20 messages per user per day.
            """

            async def run() -> dict[str, Any]:
                user_id, email = _caller(ctx)
                sent = await notifier.send_to_self(
                    user_id,
                    email,
                    require_text(subject, "subject"),
                    require_text(body, "body"),
                )
                return {"sent": True, "to": sent.to, "subject": sent.subject}

            return await act(run)

    return mcp
