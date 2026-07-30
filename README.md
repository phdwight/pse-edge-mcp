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

Serves streamable HTTP on `:8000`, with Postgres 18 as shared cache and archive. A one-shot
`migrate` service applies the Alembic schema before the app starts.

HTTP mode is **stateless with plain JSON responses by default**. This server is read-only
tools over data the freeze policy holds still, and it uses none of the features MCP sessions
exist to enable — no notifications, no resource subscriptions, no sampling, no elicitation,
no progress — so every request is self-contained. That means any replica can serve any
request behind plain round-robin: no sticky routing, no per-session memory, no event store.
Without SSE, idle clients hold no connection either, so N users stop meaning N concurrent
connections. Combined with all shared state living in Postgres, that is the whole of "any
replica, any request".

Use `--stateful` if you need MCP sessions (resumability or server-initiated messages) and
`--sse` for event-stream framing; they are independent flags. Note `--stateful` requires
clients to complete the `initialize` handshake and forces sticky routing behind a balancer.

**Postgres is optional.** Without `DATABASE_URL` the server uses an in-memory cache and keeps
no archive — the zero-config path for local stdio use, and it needs neither the `postgres`
extra nor a database. With `DATABASE_URL` set you get two things: replicas **share one cache**,
so the market-boundary freeze still means one upstream fetch per boundary however many
processes run; and every read **accumulates into an EOD archive** (daily bars and disclosures)
that deepens over time at zero extra cost to PSE Edge, which serves only limited history
itself. Nothing crawls — the archive fills solely from fetches you already made.

```bash
# applying the schema by hand, outside compose
DATABASE_URL=postgresql+asyncpg://user:pass@host/db uv run alembic upgrade head
```

## Tools

| Tool | Description |
|---|---|
| `search_companies(query)` | Find PSE-listed companies by name or ticker |
| `get_stock_quote(symbol)` | Latest EOD quote: price, change, 52-wk range, market cap, full field set |
| `get_price_history(symbol, start_date?, end_date?)` | Daily OHLC series from Edge's chart endpoint |
| `search_disclosures(symbol?, start_date?, end_date?, template?, page?)` | Disclosure metadata, market-wide or per company; 50/page with exact totals |
| `search_disclosure_fulltext(keyword, ...)` | Search the text *inside* disclosure attachments, with snippets |
| `get_disclosure(edge_no)` | One disclosure's details plus attachment and body-HTML links |
| `get_company_profile(symbol)` | Sector, incorporation, auditor, transfer agent, contacts |
| `get_financial_highlights(symbol)` | Annual + quarterly balance sheet and income statement |
| `get_dividends_and_rights(symbol)` | Declared dividends and stock rights, linked to their disclosures |
| `get_indices()` | PSEi and the 7 sector indices, with signed daily change |
| `get_market_summary()` | Index levels plus PSE Edge's homepage disclosure feeds |

Disclosure tools return metadata and links only — this server never downloads or parses
attachments, so your MCP client can fetch the returned URLs itself if it needs the files.
Note that Edge's own full-text index is partial (roughly 2023–2025 at last check), so
`search_disclosure_fulltext` is not a substitute for `search_disclosures`; it reports this
limit in its results.

Financial figures are returned exactly as PSE Edge prints them and are **never rescaled** —
Edge's own units labels are inconsistent between its annual and quarterly sections, so each
period reports its `currency_units` for you to check. Index changes are signed here even
though Edge prints them unsigned (it shows direction only as a colour and an arrow).

Roadmap (see `docs/plan.md`): OAuth 2.1 accounts with passkeys, per-user quotas, production deployment.

## Container image

Every merge to `main` publishes an image:

```bash
docker pull ghcr.io/phdwight/pse-edge-mcp:latest      # or :<version>, :sha-<sha>
# multi-arch: linux/amd64 and linux/arm64
docker run --rm -p 8000:8000 ghcr.io/phdwight/pse-edge-mcp:latest   # streamable HTTP
docker run --rm -i --entrypoint pse-edge-mcp ghcr.io/phdwight/pse-edge-mcp:latest  # stdio
```

Both architectures are gated before publishing, on native runners. The rule is
**necessity, not size**: the image must contain exactly the resolved runtime dependency
closure and nothing else — no build toolchain, no package manager, no dev dependencies, no
bytecode caches, no source tree — plus a secret scan and a smoke test that the server
starts and registers its tools. A stray dependency fails the build; a large but genuinely
required one does not. Image size is reported for information and never gated.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
```

Tests run entirely against recorded fixtures — CI never touches PSE Edge.

Work lands on `develop` and reaches `main` by pull request; `main` is protected and
requires all three CI checks (`test`, `image (amd64)`, `image (arm64)`). Bumping `version` in `pyproject.toml` makes the next merge cut
a GitHub Release with a matching immutable image tag.

## License

MIT
