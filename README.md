# pse-edge-mcp

An MCP server exposing **Philippine Stock Exchange** data from the [PSE Edge portal](https://edge.pse.com.ph/) — quotes, price history, disclosures, financial reports, and market data — to Claude and any other MCP client.

> **Unofficial.** PSE Edge has no public API; this project speaks to the same endpoints the portal's own pages use. It is not affiliated with or endorsed by the PSE. Data is provided as-is for personal/research use, with no warranty.

## Design: end-of-day by intention

To avoid loading PSE Edge during trading hours, this server is deliberately an **EOD data service** (the *market-boundary freeze* policy):

- Cached data is frozen between market session boundaries (Asia/Manila).
- The first query after the 15:00 close fetches that day's final numbers; everything until the next boundary is served from cache — shared across all users.
- **Zero upstream requests while the market is open.** Uncached queries during a session return `MARKET_OPEN_NO_CACHE` with a `retry_after` timestamp.
- Every result carries `meta.as_of`, `meta.valid_until`, and `meta.stale` so clients always know exactly how fresh the data is.

## Install (Claude Desktop / Claude Code, stdio)

```bash
uvx pse-edge-mcp
```

Claude Desktop config:

```json
{
  "mcpServers": {
    "pse-edge": { "command": "uvx", "args": ["pse-edge-mcp"] }
  }
}
```

## Run with Docker Compose (HTTP + Postgres)

```bash
cp .env.example .env   # set POSTGRES_PASSWORD
docker compose up --build
```

Serves streamable HTTP on `:8000`, with Postgres 18 as shared cache/archive.

## Tools (Phase 1)

| Tool | Description |
|---|---|
| `search_companies(query)` | Find PSE-listed companies by name or ticker |
| `get_stock_quote(symbol)` | Latest EOD quote: price, change, 52-wk range, market cap, full field set |
| `get_price_history(symbol, start_date?, end_date?)` | Daily OHLC series from Edge's chart endpoint |

Roadmap (see `docs/plan.md`): disclosures search + documents, financial reports, dividends, indices & market summary, OAuth 2.1 accounts with passkeys, remote deployment.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
```

Tests run entirely against recorded fixtures — CI never touches PSE Edge.

## License

MIT
