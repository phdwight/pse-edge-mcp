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

## Tools

| Tool | Description |
|---|---|
| `search_companies(query)` | Find PSE-listed companies by name or ticker |
| `get_stock_quote(symbol)` | Latest EOD quote: price, change, 52-wk range, market cap, full field set |
| `get_price_history(symbol, start_date?, end_date?)` | Daily OHLC series from Edge's chart endpoint |
| `search_disclosures(symbol?, start_date?, end_date?, template?, page?)` | Disclosure metadata, market-wide or per company; 50/page with exact totals |
| `search_disclosure_fulltext(keyword, ...)` | Search the text *inside* disclosure attachments, with snippets |
| `get_disclosure(edge_no)` | One disclosure's details plus attachment and body-HTML links |

Disclosure tools return metadata and links only — this server never downloads or parses
attachments, so your MCP client can fetch the returned URLs itself if it needs the files.
Note that Edge's own full-text index is partial (roughly 2023–2025 at last check), so
`search_disclosure_fulltext` is not a substitute for `search_disclosures`; it reports this
limit in its results.

Roadmap (see `docs/plan.md`): financial reports, dividends, indices & market summary, Postgres archive, OAuth 2.1 accounts with passkeys, remote deployment.

## Container image

Every merge to `main` publishes an image:

```bash
docker pull ghcr.io/phdwight/pse-edge-mcp:latest      # or :<version>, :sha-<sha>
# multi-arch: linux/amd64 and linux/arm64
docker run --rm -p 8000:8000 ghcr.io/phdwight/pse-edge-mcp:latest   # streamable HTTP
docker run --rm -i --entrypoint pse-edge-mcp ghcr.io/phdwight/pse-edge-mcp:latest  # stdio
```

Both architectures are gated before publishing: <200 MB, secret-scanned, and
smoke-tested on native runners.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
```

Tests run entirely against recorded fixtures — CI never touches PSE Edge.

Work lands on `develop` and reaches `main` by pull request; `main` is protected and
requires both CI checks. Bumping `version` in `pyproject.toml` makes the next merge cut
a GitHub Release with a matching immutable image tag.

## License

MIT
