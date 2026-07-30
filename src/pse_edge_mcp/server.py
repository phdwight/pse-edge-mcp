"""FastMCP server wiring — Phase 1 tools: search_companies, get_stock_quote, get_price_history."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

try:  # MCP SDK >= 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # older SDKs shipped the same API as FastMCP
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from .client import PseEdgeClient
from .config import Settings
from .errors import PseEdgeMcpError, SymbolNotFoundError
from .market_calendar import MarketCalendar
from .models import CompanyHit, Meta, OhlcBar, PriceHistory, StockQuote
from .parsers import parse_stock_data_page
from .service import FreezeService

INSTRUCTIONS = """Data source: PSE Edge (Philippine Stock Exchange disclosure portal, unofficial).
This server serves END-OF-DAY data by design: values are frozen between market
sessions to avoid loading PSE Edge during trading hours (Asia/Manila). Every
result carries meta.as_of / meta.valid_until; meta.stale=true means the market
is open and you are seeing the latest end-of-day values. If you get a
MARKET_OPEN_NO_CACHE error, retry after the market closes (15:00 Manila)."""


def build_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or Settings.from_env()
    calendar = MarketCalendar(
        tz=settings.market_tz, open_time=settings.market_open, close_time=settings.market_close
    )
    client = PseEdgeClient(settings)
    service = FreezeService(calendar=calendar)

    mcp = MCPServer("pse-edge", instructions=INSTRUCTIONS)

    def _result(payload: Any, meta: Meta) -> dict:
        return {"data": payload, "meta": meta.model_dump(mode="json")}

    async def _resolve(symbol: str) -> dict:
        """Symbol -> parsed stockData page fields (company_id, security_id, quote...)."""
        sym = symbol.strip().upper()
        served = await service.get(
            f"autocomplete:{sym}", lambda: client.search_companies(sym)
        )
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
    async def search_companies(query: str) -> dict:
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
    async def get_stock_quote(symbol: str) -> dict:
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
    ) -> dict:
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

    return mcp
