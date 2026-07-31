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

## Connecting to a hosted server

A deployment with auth on is a normal OAuth 2.1 protected resource, so a modern MCP client
needs only the URL — it discovers everything else and drives the whole flow itself.

```json
{
  "mcpServers": {
    "pse-edge": { "url": "https://your-host.example.com/mcp" }
  }
}
```

### What happens on first connect

Nothing here is manual except the two browser steps in bold.

1. The client `POST`s to `/mcp` with no token and gets **401** carrying
   `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`.
   That header is the entire bootstrap: it tells the client where to look next.
2. It fetches that document, learns which authorization server guards this resource, then
   reads `/.well-known/oauth-authorization-server` for the endpoints.
3. It registers itself at `/oauth/register` (RFC 7591) — no client secret, no operator
   involvement, no pre-shared credentials. It gets back a `client_id`.
4. It opens `/oauth/authorize` in a browser with a PKCE challenge (S256 required).
5. **The user signs up or signs in.** New users land on `/signup`, give an email, and
   receive a link; following it enrolls **a passkey** at `/enroll`. Returning users hit
   `/login` and use the passkey they already have. No password exists anywhere in the system.
6. **The user approves the client** on a consent screen naming it.
7. The browser returns to the client with a single-use code; the client exchanges it at
   `/oauth/token` with its PKCE verifier and receives an access token (30 min) and a refresh
   token (30 days).
8. The client calls `/mcp` with `Authorization: Bearer …` and refreshes silently from then
   on. The user is not asked again.

```
client ──POST /mcp──────────────▶ 401 + WWW-Authenticate
       ──GET  /.well-known/… ───▶ metadata
       ──POST /oauth/register ──▶ client_id
       ──GET  /oauth/authorize ─▶ browser: signup/login → passkey → consent
       ◀─────────────────────────  ?code=…
       ──POST /oauth/token ─────▶ access (30m) + refresh (30d)
       ──POST /mcp + Bearer ────▶ tools
```

Refresh tokens rotate on every use, and replaying a rotated one revokes that whole session
family (RFC 9700 §4.14) — a stolen refresh token gets one use before the theft is detected
and the session dies.

### If your client does not do OAuth yet

The operator issues a token directly, and the user pastes it into a header. Same server, no
browser:

```bash
pse-edge-admin create-user you@example.com
pse-edge-admin issue-token you@example.com --note laptop   # plaintext shown once
```

```bash
curl -X POST https://your-host.example.com/mcp \
  -H "Authorization: Bearer pse_..." \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

This is also the only route on a LAN-only deployment: passkeys need a secure context, so
plain http cannot enroll one.

### What a user can see and remove

`/account` shows everything held about them — email, passkeys, active tokens, hourly usage
counts. `POST /account/delete` erases it immediately and completely, with no approval step.
`/privacy` states what is collected and for how long. Usage counts are deleted after 90 days.

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

### Bearer auth and quotas (opt-in)

Set `PSE_AUTH_REQUIRED=1` (needs `DATABASE_URL`) and every HTTP request must carry
`Authorization: Bearer <token>`. Users arrive either way described in
[Connecting to a hosted server](#connecting-to-a-hosted-server) — self-service through
OAuth 2.1 and passkeys, or an operator-issued token. PKCE is mandatory (S256 only) and no
password exists anywhere in the system.

Operators get `pse-edge-admin delete-user` and `purge-usage` (cron the latter daily), and
`delete-user` uses the same erasure code path as the user's own delete button, so the two
cannot drift apart.

Tokens are opaque and stored only as SHA-256 hashes. Revocation
(`pse-edge-admin revoke-token …` / `disable-user …`) takes effect within the validation
cache's TTL — 60 s by default (`PSE_TOKEN_CACHE_TTL`), which is precisely the
revocation-latency budget. Per-user quotas (default 60/min, 2,000/day, overridable per
user) are counted in-process and answer HTTP 429 with `Retry-After`; with N replicas the
effective ceiling is up to N× nominal, which is fine for abuse prevention. stdio mode
never authenticates — it runs on your own machine.

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

All six delivery phases are complete; see `docs/plan.md` for what each decided.

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

## Production

Two topologies, chosen by how the host is reached. Both pull the published image rather than
building, so production runs the artifact CI gated.

**A host reachable on ports 80 and 443** — Caddy terminates TLS and renews certificates
automatically. It publishes on 8280/8243 to stay clear of a NAS's own web UI, so the router
must forward 80 → 8280 and 443 → 8243; certificate authorities always validate on 80/443, so
that forwarding is required, not optional:

```bash
cp .env.example .env    # PSE_DOMAIN, PSE_ACME_EMAIL, POSTGRES_PASSWORD, ZEPTOMAIL_API_KEY, PSE_IMAGE_TAG
docker compose -f compose.prod.yaml up -d
```

**A NAS or any host behind a home router**, in two stages. Stage 1 is LAN-only and needs
nothing from Cloudflare:

```bash
docker compose -f compose.nas.yaml up -d                                  # http://<nas-ip>:8200
docker compose -f compose.nas.yaml -f compose.tunnel.yaml up -d           # + public hostname
```

The tunnel overlay starts `cloudflared`, which dials *out* — so there is no port forwarding
and nothing for CGNAT to break. Set `PSE_LAN_BIND=127.0.0.1` alongside it to move the stage 1
LAN port onto loopback; Compose merges `ports` additively, so an overlay can add a mapping
but never remove one.

Both give auth on by default, daily backups, a daily retention purge, and no published
database port. Health probes are `/health` (liveness) and `/health/ready` (readiness). The
app is importable for other servers: `uvicorn pse_edge_mcp.asgi:app --workers 4`.

See **[docs/deploy.md](docs/deploy.md)** for both guides, including the two settings most
worth getting right: pin `PSE_IMAGE_TAG` rather than tracking `:latest`, and make
`PSE_PUBLIC_URL` the real external https URL, because WebAuthn binds every passkey to the
origin it was enrolled under.

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
