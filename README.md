# pse-edge-mcp

[![CI](https://github.com/phdwight/pse-edge-mcp/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/phdwight/pse-edge-mcp/actions/workflows/ci.yml)
[![Release](https://github.com/phdwight/pse-edge-mcp/actions/workflows/release.yml/badge.svg)](https://github.com/phdwight/pse-edge-mcp/actions/workflows/release.yml)
[![PyPI](https://img.shields.io/pypi/v/pse-edge-mcp)](https://pypi.org/project/pse-edge-mcp/)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

An MCP server exposing **Philippine Stock Exchange** data from the [PSE Edge portal](https://edge.pse.com.ph/) — quotes, price history, disclosures, financial reports, and market data — to Claude and any other MCP client.

> **Unofficial.** PSE Edge has no public API; this project speaks to the same endpoints the portal's own pages use. It is not affiliated with or endorsed by the PSE. Data is provided as-is for personal/research use, with no warranty.

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [Design: end-of-day prices, fetch-once everything else](#design-end-of-day-prices-fetch-once-everything-else)
- [Tools, resources, prompts](#tools-resources-prompts)
- [Architecture](#architecture)
- [Golden path: one request traced](#golden-path-one-request-traced)
- [Connecting to a hosted server](#connecting-to-a-hosted-server)
- [Run with Docker Compose](#run-with-docker-compose-http--postgres)
- [Configuration](#configuration)
- [Container image](#container-image)
- [Production](#production)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Features

- **13 read tools + 1 action tool** covering quotes, price history, disclosures (metadata, full-text, and detail), company profiles, financials, dividends, indices, and market summary — plus an attachment **resource**, two **prompts** with symbol completion, and tool annotations.
- **Deliberately gentle on PSE Edge:** every unique query hits it at most once per day, and prices follow a strict market-boundary freeze.
- **OAuth 2.1 + passkeys** for humans (no passwords anywhere), **`client_credentials`** for headless agents — both opt-in; stdio needs nothing.
- **Postgres optional:** zero-config in-memory for local stdio, or a shared cache + an ever-deepening EOD archive when `DATABASE_URL` is set.
- **Loud on drift:** a nightly canary validates live pages against the real models and alerts only on failure; a restyled page raises an error, never partial data.
- **Multi-arch container image** (amd64 + arm64), gated on necessity — the image contains exactly the runtime dependency closure and nothing else.

## Quick start

**Claude Desktop / Claude Code (stdio):**

```bash
uvx pse-edge-mcp
```

```json
{
  "mcpServers": {
    "pse-edge": { "command": "uvx", "args": ["pse-edge-mcp"] }
  }
}
```

**A hosted deployment** (auth on) is a normal OAuth 2.1 protected resource — modern clients need only the URL and drive the whole flow themselves ([details below](#connecting-to-a-hosted-server)):

```json
{
  "mcpServers": {
    "pse-edge": { "url": "https://your-host.example.com/mcp" }
  }
}
```

**Docker (HTTP + Postgres):**

```bash
cp .env.example .env   # set POSTGRES_PASSWORD
docker compose up --build
```

## Design: end-of-day prices, fetch-once everything else

Four layers, one direction of dependency. Every read goes through `FreezeService.get()` with an explicit per-domain policy — no tool ever touches the HTTP client directly. ★ marks the invariant the whole design exists to protect, and it guards **prices only**.

```mermaid
flowchart TD
    C["MCP client"] -- "tool call" --> S["<b>server.py</b> · MCP boundary<br/>validate args · delegate · shape reply"]
    S --> R["<b>repositories.py</b> · one per domain<br/>cache key · freeze read · parse · endpoint routing"]
    R --> F["<b>service.py</b> · FreezeService ★<br/>3 policies · miss → fetch once"]
    F --> P["<b>client.py</b> · PseEdgeClient<br/>throttled HTTPS · single-flight · 2 dialects"]
    P --> E["PSE Edge<br/>edge.pse.com.ph"]
```

**★ Market-boundary freeze — prices only.** A cached stock price is never refetched while the market is open (09:30–15:00 Asia/Manila, trading days) — the last close answers, flagged `stale`. A price *nobody has ever asked for* is the one exception: fetched once mid-session and served as identity + `previous_close` only (every session-moving field withheld), with `stale: true` plus a `meta.note` saying it is not a realtime value; the settled figures replace it after the close.

| Policy | Applies to | Behaviour |
|---|---|---|
| `EOD-frozen` ★ | `get_stock_quote`, `get_price_history` | A cached price is never refetched during a session; a never-cached key is fetched once and surfaces only `previous_close`, labelled not-realtime for the whole session. Also the **default** policy, so an unlabelled read can only over-protect PSE Edge. |
| `daily-refresh` | Companies, disclosures, profiles, financials, dividends, indices, summary | First ask fetches at any hour — once, deduplicated across concurrent callers; every repeat of the same query answers from storage until the next 15:00 close. |
| `immutable` | Disclosure detail by `edge_no`, attachment bytes | The object never changes upstream. Fetched once ever; `valid_until: null`. |

If PSE Edge is unreachable and an expired entry exists, tools serve it flagged `meta.stale: true` rather than discarding real data for an error. `EDGE_UNAVAILABLE` means unreachable *and* nothing cached.

Every data tool returns the same envelope — `meta` is the freshness contract:

```jsonc
{
  "data": { /* …StockQuote… */ },
  "meta": {
    "as_of":       "2026-08-06T15:00:00+08:00",  // ISO-8601, Asia/Manila
    "valid_until": "2026-08-07T15:00:00+08:00",  // null when immutable
    "from_cache":  false,
    "stale":       false,                        // true = not a settled EOD value
    "data_policy": "EOD-frozen",                 // "daily-refresh" / "immutable" elsewhere
    "note":        null                          // freshness caveat, e.g. "not a realtime value"
  }
}
```

## Tools, resources, prompts

| Tool | Description |
|---|---|
| `search_companies(query)` | Find PSE-listed companies by name or ticker |
| `validate_symbol(symbol)` | Cheap yes/no check that a ticker exists, with its company name and id |
| `get_stock_quote(symbol)` | Latest EOD quote: price, change, 52-wk range, market cap, full field set |
| `get_price_history(symbol, start_date?, end_date?)` | Daily OHLC series from Edge's chart endpoint |
| `search_disclosures(symbol?, start_date?, end_date?, template?, page?)` | Disclosure metadata, market-wide or per company; 50/page with exact totals |
| `search_disclosure_fulltext(keyword, ...)` | Search the text *inside* disclosure attachments, with snippets |
| `get_disclosure(edge_no, max_files?)` | One disclosure's details plus attachment and body-HTML links; attachments capped at `max_files` (default 20) with an honest truncation flag. Each attachment carries a `resource_uri` — read the file's bytes via MCP `resources/read` (`pse-edge://attachment/<file_id>`, cached immutably, 10 MB cap) |
| `get_company_profile(symbol)` | Sector, incorporation, auditor, transfer agent, contacts |
| `get_financial_highlights(symbol)` | Annual + quarterly balance sheet and income statement |
| `get_dividends_and_rights(symbol)` | Declared dividends and stock rights, linked to their disclosures |
| `get_indices()` | PSEi and the 7 sector indices, with signed daily change |
| `get_market_summary()` | Index levels plus PSE Edge's homepage disclosure feeds |
| `get_server_version()` | The deployed version of this MCP server itself (matches `/health`) |
| `send_email(subject, body)` | Email **yourself** a note (auth-enabled deployments only) |

Beyond tools, the server exposes the attachment **resource** above, two **prompts** (`market_recap`, `company_briefing(symbol)` — the symbol argument autocompletes from PSE Edge's own lookup), and MCP **tool annotations** so hosts can auto-approve the read-only tools. It is described for the MCP Registry in [`server.json`](server.json).

`send_email` is the only tool that acts rather than reads. **It has no recipient argument**: the message always goes to the account that authenticated the session, so it cannot be used as a relay and there is nothing for prompt injection to redirect — which matters because this server returns disclosure text the operator does not control. It appears only on deployments with auth enabled (there is no verified address otherwise), the body is escaped rather than rendered as HTML, and it is capped at 20 messages per user per day.

Disclosure tools return metadata and links only — this server never downloads or parses attachments (beyond the explicit resource read), so your MCP client can fetch the returned URLs itself if it needs the files. Note that Edge's own full-text index is partial (roughly 2023–2025 at last check), so `search_disclosure_fulltext` is not a substitute for `search_disclosures`; it reports this limit in its results.

Financial figures are returned exactly as PSE Edge prints them and are **never rescaled** — Edge's own units labels are inconsistent between its annual and quarterly sections, so each period reports its `currency_units` for you to check. Index changes are signed here even though Edge prints them unsigned (it shows direction only as a colour and an arrow).

## Architecture

### The layers

| Layer | Owns | Never |
|---|---|---|
| `server.py` — MCP boundary | Argument validation, delegation, reply shaping. Error mapping happens once in `reply()`; action tools go through `act()` (no freshness `meta`). | Domain logic, cache keys, parsing, endpoint choices |
| `repositories.py` — domain layer | One repository per data domain: the cache key, the freeze read, the parse, the Pydantic model. **Endpoint routing lives here.** | Depending on the concrete client — only on the protocols below |
| `service.py` / `sources.py` — policy & seams | `FreezeService` enforces the per-read policy and wraps every result in `Served[T]` (value + `as_of`, `valid_until`, `from_cache`, `stale`). `sources.py` declares the five narrow per-domain source protocols; `FrozenCache` is the cache seam. | — |
| `client.py` / `parsers.py` — edge of the world | Pure HTTP, MCP-agnostic: token-bucket throttle, single-flight, retries; two request dialects (JSON-body POST for chart `.ax` endpoints, form-encoded POST returning HTML fragments for `search.ax`). Parsers turn HTML/JSON into dicts; any shape drift raises `EndpointChangedError` — loud, never partial. | — |

### Core class map

Five repositories cover the whole tool surface. Each consumes a narrow source protocol — the concrete client satisfies all five, but no repository knows that, so each is testable with a few-line fake and no HTTP mocking.

| Repository | Methods → models | Consumes | Policy / note |
|---|---|---|---|
| `CompanyRepository` | `search`, `resolve`, `try_resolve` → `CompanyHit` | `CompanySource` | `daily-refresh` · resolves symbol → `company_id` for every other repo |
| `QuoteRepository` | `quote` → `StockQuote` · `history` → `PriceHistory` | `QuoteSource` | `EOD-frozen` ★ — the only market-gated domain · bars archived |
| `DisclosureRepository` | `search`, `fulltext`, `detail`, `attachment` | `DisclosureSource` | searches `daily-refresh`; `detail`/`attachment` `immutable` |
| `CompanyInfoRepository` | `profile`, `financials`, `dividends_and_rights` | `CompanyInfoSource` | `daily-refresh` · financial units passed through verbatim |
| `MarketRepository` | `indices`, `summary` | `MarketSource` | `daily-refresh` · index signs derived from Edge's ▲/▼ glyph |
| `NotificationService` *(action)* | `send(user, subject, body)` → `SentEmail` | — | recipient comes from the **bearer token**, never an argument |

### Protocols and swappable implementations

One switch picks the column: `DATABASE_URL` unset → in-memory / Null; set → Postgres. Postgres modules import lazily, so a lean install never pays for them.

| Protocol | `DATABASE_URL` unset | `DATABASE_URL` set |
|---|---|---|
| `Storage` | `InMemoryStorage` | `PostgresStorage` |
| `Archive` | `NullArchive` | `PostgresArchive` |
| `UsageSink` | `NullUsageRecorder` | `PostgresUsageSink` |
| `AuthStore` | — | `PostgresAuthStore` |
| `EmailSender` | `ConsoleEmailSender` | `ZeptoMailSender` (when `ZEPTOMAIL_API_KEY` is set) |

### HTTP composition — built once, in `asgi.py`

```mermaid
flowchart LR
    H["HealthApp<br/>/health · /health/ready"] --> A["AuthApp<br/>/oauth/* · signup · /account · /privacy"]
    A --> M["AuthMiddleware<br/>bearer validation · quotas · usage"]
    M --> MCP["MCP app<br/>the tool surface"]
```

`/health` is liveness and never touches the database; `/health/ready` is readiness. Behind `AuthApp`: `OAuthService` (DCR · PKCE-only · refresh families), `PasskeyService` (WebAuthn + web sessions), `TokenService` (opaque `pse_` tokens, SHA-256 at rest).

### Error family — one root, mapped once in `reply()`

| Error | Meaning |
|---|---|
| `SymbolNotFoundError` | `SYMBOL_NOT_FOUND` |
| `InvalidArgumentError` | `INVALID_ARGUMENT` — bad input, caught at the boundary |
| `EndpointChangedError` | Edge redesigned a page — loud, never partial |
| `EdgeUnavailableError` | Upstream unreachable **and** nothing cached |
| `MarketOpenNoCacheError` | Retained for client compatibility; no longer raised |
| `ActionUnavailableError` | Action tool needs auth enabled |
| `ActionRateLimitedError` | 20 emails / user / day |

### Watchdog

A nightly canary (`pse-edge-canary`, plus a compose service) fetches live pages **bypassing the cache** and validates the same Pydantic models the repositories build — a 200 with a restyled table is exactly the failure it exists to catch. It still refuses to run while the market is open (the ★ invariant outranks it), emails `PSE_OPERATOR_EMAIL` **only on failure**, and exits non-zero so cron notices.

## Golden path: one request traced

`get_stock_quote("SM")` after market close, cold cache:

1. **`server.py`** — `validation.py` checks the symbol shape (bad input → `INVALID_ARGUMENT`), then `reply()` wraps the repository call — the only place errors become MCP error payloads.
2. **`QuoteRepository.quote("SM")`** — resolves `SM` → `company_id` through `CompanyRepository`, picks the endpoint, builds the cache key. Tools never see any of this.
3. **`FreezeService.get(key, fetch, policy="EOD-frozen")` ★** — fresh cache entry → serve it. Market open + cached → serve the last close flagged `stale`, never refetch. Market open + never cached → fetch once, label `stale: true` + `note` for the whole session. Market closed + miss → fetch. Fetch fails but an expired entry exists → serve it flagged `stale`.
4. **`PseEdgeClient.fetch_stock_data_page(company_id)`** — token bucket (1 req/s), single-flight dedupe, retries. Wire dates are `MM-dd-yyyy`; the JSON-vs-form dialect is chosen per endpoint.
5. **`parsers.py` → `StockQuote`** — HTML → dict → validated Pydantic model. Any drift in Edge's markup raises `EndpointChangedError`.
6. **`cache.py` / `archive.py`** — the entry freezes until the next 15:00 close; daily bars archive opportunistically (a dead database never fails a read).

## Connecting to a hosted server

A deployment with auth on is a normal OAuth 2.1 protected resource, so a modern MCP client needs only the URL — it discovers everything else and drives the whole flow itself.

### What happens on first connect

Nothing here is manual except the two browser steps in bold.

1. The client `POST`s to `/mcp` with no token and gets **401** carrying `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`. That header is the entire bootstrap: it tells the client where to look next.
2. It fetches that document, learns which authorization server guards this resource, then reads `/.well-known/oauth-authorization-server` for the endpoints.
3. It registers itself at `/oauth/register` (RFC 7591) — no client secret, no operator involvement, no pre-shared credentials. It gets back a `client_id`.
4. It opens `/oauth/authorize` in a browser with a PKCE challenge (S256 required).
5. **The user signs up or signs in.** New users land on `/signup`, agree to the (deliberately tiny) data policy, give an email, and receive a link; the link shows a confirm page whose button enrolls **a passkey** at `/enroll` — the confirm step exists so a mail scanner's prefetch cannot spend the link. Returning users hit `/login` and use the passkey they already have. No password exists anywhere in the system.
6. **The user approves the client** on a consent screen naming it.
7. The browser returns to the client with a single-use code; the client exchanges it at `/oauth/token` with its PKCE verifier and receives an access token (15 min) and a single-use refresh token (24 h).
8. The client calls `/mcp` with `Authorization: Bearer …` and refreshes silently from then on. The user is not asked again.

```
client ──POST /mcp──────────────▶ 401 + WWW-Authenticate
       ──GET  /.well-known/… ───▶ metadata
       ──POST /oauth/register ──▶ client_id
       ──GET  /oauth/authorize ─▶ browser: signup/login → passkey → consent
       ◀─────────────────────────  ?code=…
       ──POST /oauth/token ─────▶ access (15 min) + refresh (24 h)
       ──POST /mcp + Bearer ────▶ tools
```

Refresh tokens rotate on every use, and replaying a rotated one revokes that whole session family (RFC 9700 §4.14) — a stolen refresh token gets one use before the theft is detected and the session dies.

### Headless agents (`client_credentials`)

For a LangGraph app, the Anthropic Messages API MCP connector, or any agent that cannot open a browser. No redirect, no passkey, no consent screen — a client id and secret.

**1. Provision.** Two routes, same result:

- **From the web** (needs no shell — the practical choice on a NAS): set `PSE_ADMIN_EMAILS` to your account's email, sign in, and a **Machine clients** panel appears on `/account` with create and revoke controls. Access is gated to that allowlist — a normal signup never sees it.
- **From the CLI:** `pse-edge-admin create-machine-client --name langgraph-app`.

Either way `client_id` and `client_secret` are shown **once**. Only the secret's SHA-256 is stored, so it cannot be recovered — only revoked and reissued (from the same account page, or `pse-edge-admin revoke-machine-client <client_id>`).

**2. Mint a token:**

```bash
curl -s -X POST https://pse.sakayandgo.com/oauth/token \
  -d grant_type=client_credentials \
  -d client_id=$CLIENT_ID -d client_secret=$CLIENT_SECRET \
  -d scope=mcp -d resource=https://pse.sakayandgo.com/mcp
```

```json
{"access_token": "pse_…", "token_type": "Bearer", "expires_in": 3600, "scope": "mcp"}
```

HTTP Basic works too (`curl -u "$CLIENT_ID:$CLIENT_SECRET"`), which is what most SDKs send. **No refresh token is issued** — the client already holds a long-lived secret and simply re-requests when the hour is up.

**3. Use it:**

```bash
curl -s -X POST https://pse.sakayandgo.com/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

Revoke with `pse-edge-admin revoke-machine-client <client_id>`, which kills the secret, every token it minted, and the backing service account in one step.

> **Registering does not grant this.** `/oauth/register` is open to the internet, so a client that registers itself — even declaring `grant_types: ["client_credentials"]` and sending a secret — is refused with `unauthorized_client`. Authorization comes from a `client_type` column only the admin CLI writes, never from anything a registrant says about itself.

Give each agent **its own machine client**: quotas are per client, so a runaway job throttles itself, and revoking one does not touch the others.

**Building an app on top of this?** `examples/langgraph_client.py` is a working client for the multi-tenant case — your app authenticates as *itself* with one machine client, your users never see this server. It carries an `httpx.Auth` that mints and refreshes the 1-hour token (verified: concurrent calls mint once; a stale token recovers on 401), plus the agent instructions worth pasting into a system prompt. Note it needs `mcp<2` — `langchain-mcp-adapters` does not yet import against the 2.x SDK.

### If your client does not do OAuth yet

The operator issues a token directly, and the user pastes it into a header. Same server, no browser:

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

This is also the only route on a LAN-only deployment: passkeys need a secure context, so plain http cannot enroll one.

### What a user can see and remove

`/account` shows everything held about them — email, passkeys, active tokens, hourly usage counts. `POST /account/delete` erases it immediately and completely, with no approval step. `/privacy` states what is collected and for how long. Usage counts are deleted after 90 days.

## Run with Docker Compose (HTTP + Postgres)

```bash
cp .env.example .env   # set POSTGRES_PASSWORD
docker compose up --build
```

Serves streamable HTTP on `:8000`, with Postgres 18 as shared cache and archive. A one-shot `migrate` service applies the Alembic schema before the app starts.

HTTP mode is **stateless with plain JSON responses by default**. This server is read-only tools over data the freeze policy holds still, and it uses none of the features MCP sessions exist to enable — no notifications, no resource subscriptions, no sampling, no elicitation, no progress — so every request is self-contained. That means any replica can serve any request behind plain round-robin: no sticky routing, no per-session memory, no event store. Without SSE, idle clients hold no connection either. Use `--stateful` if you need MCP sessions and `--sse` for event-stream framing; they are independent flags.

### Bearer auth and quotas (opt-in)

Set `PSE_AUTH_REQUIRED=1` (needs `DATABASE_URL`) and every HTTP request must carry `Authorization: Bearer <token>`. Users arrive either way described in [Connecting to a hosted server](#connecting-to-a-hosted-server) — self-service through OAuth 2.1 and passkeys, or an operator-issued token. PKCE is mandatory (S256 only) and no password exists anywhere in the system.

Tokens are opaque and stored only as SHA-256 hashes. Revocation (`pse-edge-admin revoke-token …` / `disable-user …`) takes effect within the validation cache's TTL — 60 s by default, which is precisely the revocation-latency budget. Per-user quotas (default 60/min, 2,000/day, overridable per user) are counted in-process and answer HTTP 429 with `Retry-After`; with N replicas the effective ceiling is up to N× nominal, which is fine for abuse prevention. stdio mode never authenticates — it runs on your own machine.

Operators get `pse-edge-admin delete-user` and `purge-usage` (cron the latter daily), and `delete-user` uses the same erasure code path as the user's own delete button, so the two cannot drift apart.

**Postgres is optional.** Without `DATABASE_URL` the server uses an in-memory cache and keeps no archive — the zero-config path for local stdio use. With it set, replicas **share one cache** (the freeze still means one upstream fetch per boundary however many processes run), and every read **accumulates into an EOD archive** (daily bars and disclosures) that deepens over time at zero extra cost to PSE Edge. Nothing crawls — the archive fills solely from fetches you already made.

```bash
# applying the schema by hand, outside compose
DATABASE_URL=postgresql+asyncpg://user:pass@host/db uv run alembic upgrade head
```

## Configuration

Everything is environment-sourced into one frozen `Settings` object. Two variables change the shape of the system: `DATABASE_URL` picks the [storage column](#protocols-and-swappable-implementations), and `PSE_AUTH_REQUIRED` turns on the whole auth stack (and the `send_email` tool with it).

| Variable | Default | What it governs |
|---|---|---|
| **Upstream — protect PSE Edge** | | |
| `PSE_EDGE_BASE_URL` | `https://edge.pse.com.ph` | Upstream portal root |
| `PSE_THROTTLE_RPS` / `PSE_THROTTLE_BURST` | `1.0` / `2` | Token-bucket rate toward Edge |
| `PSE_TIMEOUT_SEC` / `PSE_RETRY_ATTEMPTS` | `20` / `3` | Per-request timeout and retries |
| **Storage — the one switch** | | |
| `DATABASE_URL` | unset | Unset → in-memory cache + `NullArchive`. Set → shared Postgres cache + archive + auth tables (schema via Alembic only) |
| `PSE_DB_POOL_SIZE` / `PSE_DB_MAX_OVERFLOW` | `5` / `10` | Connection pool |
| **Auth — opt-in, needs `DATABASE_URL`** | | |
| `PSE_AUTH_REQUIRED` | `0` | Bearer auth + quotas + OAuth/passkeys; stdio never authenticates |
| `PSE_TOKEN_CACHE_TTL` | `60` | The revocation-latency budget — nothing else |
| `PSE_QUOTA_PER_MIN` / `PSE_QUOTA_PER_DAY` | `60` / `2000` | Per-user quotas, counted in-process (per worker) |
| `PSE_PUBLIC_URL` | `http://localhost:8000` | Real external https URL — drives WebAuthn rp_id, email links, OAuth issuer; a wrong value breaks passkeys |
| `PSE_ACCESS_TTL_MIN` / `PSE_REFRESH_TTL_HOURS` | `15` / `24` | Token lifetimes; the refresh token is single-use and reuse revokes the family. `PSE_REFRESH_UNUSED_TTL_HOURS` (24) caps a family that never rotated |
| `PSE_ADMIN_EMAILS` | empty | Operator allowlist for the `/account` machine-client panel; never derived from user input |
| **Email & operations** | | |
| `ZEPTOMAIL_API_KEY` | unset | Unset → `ConsoleEmailSender`; set → ZeptoMail |
| `PSE_EMAIL_FROM` | `no-reply@localhost` | Sender address (ZeptoMail verifies **exact** domains) |
| `PSE_OPERATOR_EMAIL` | unset | Canary failure alerts — failures only, never "all fine" |
| `PSE_USAGE_RETENTION_DAYS` | `90` | Usage log retention (aggregated per user-hour, never per request) |
| **Server** | | |
| `PSE_STATEFUL` / `PSE_SSE` | `0` / `0` | MCP session & response mode |
| `PSE_LOG_JSON` / `PSE_LOG_LEVEL` | `0` / `INFO` | Both formatters timestamp and redact; INFO logs refusals only |

## Container image

Every merge to `main` publishes an image:

```bash
docker pull ghcr.io/phdwight/pse-edge-mcp:latest      # or :<version>, :sha-<sha>
# multi-arch: linux/amd64 and linux/arm64
docker run --rm -p 8000:8000 ghcr.io/phdwight/pse-edge-mcp:latest   # streamable HTTP
docker run --rm -i --entrypoint pse-edge-mcp ghcr.io/phdwight/pse-edge-mcp:latest  # stdio
```

Both architectures are gated before publishing, on native runners. The rule is **necessity, not size**: the image must contain exactly the resolved runtime dependency closure and nothing else — no build toolchain, no package manager, no dev dependencies, no bytecode caches, no source tree — plus a secret scan and a smoke test that the server starts and registers its tools. A stray dependency fails the build; a large but genuinely required one does not. Image size is reported for information and never gated.

## Production

One file, `compose.nas.yaml`, for a NAS or any single Docker host, in two stages. It pulls the published image rather than building, so production runs the artifact CI gated. Stage 1 is LAN-only and needs nothing from Cloudflare:

```bash
docker compose -f compose.nas.yaml up -d                     # http://<nas-ip>:8200
docker compose -f compose.nas.yaml --profile tunnel up -d    # + public hostname
```

The `tunnel` profile starts `cloudflared`, which dials *out* — so there is no port forwarding, no ACME, and nothing for CGNAT to break; Cloudflare terminates TLS at its edge. Set `CLOUDFLARE_TUNNEL_TOKEN`, `PSE_PUBLIC_URL` and `PSE_LAN_BIND=127.0.0.1` in `.env` alongside it — the last moves the stage 1 LAN port onto loopback, which is the only way to unpublish it, because Compose merges `ports` additively.

Both stages give auth on by default, daily backups, a daily retention purge, and no published database port. Health probes are `/health` (liveness) and `/health/ready` (readiness). The app is importable for other servers: `uvicorn pse_edge_mcp.asgi:app --workers 4`.

See **[docs/deploy.md](docs/deploy.md)** for the full guide, including the two settings most worth getting right: pin `PSE_IMAGE_TAG` rather than tracking `:latest`, and make `PSE_PUBLIC_URL` the real external https URL, because WebAuthn binds every passkey to the origin it was enrolled under.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
```

Tests run entirely against recorded fixtures — CI never touches PSE Edge.

**New to the codebase?** [docs/walkthrough.md](docs/walkthrough.md) is the developer and architect walkthrough: the request lifecycle, the freeze policy, the layering, how to add a tool or a whole data domain, and a symptom-to-cause debugging table. Also available as [a PDF](docs/walkthrough.pdf). For the one-page visual version of the [Architecture](#architecture) section — classes, protocols, the data path, the config matrix — open [docs/reference-card.html](docs/reference-card.html) in any browser; it is fully self-contained and works offline. Every design decision is recorded in [docs/plan.md](docs/plan.md), and the verified endpoint map lives in [docs/endpoints.md](docs/endpoints.md).

## Contributing

Issues and pull requests are welcome. The ground rules:

- Work lands on `develop` and reaches `main` by pull request; `main` is protected and requires all three CI checks (`test`, `image (amd64)`, `image (arm64)`).
- Tests never touch PSE Edge — new endpoints need new recorded fixtures in `tests/fixtures/`.
- New tools follow the layering above: a new data domain is a new repository plus thin tools, never fetch/parse logic in `server.py`.
- Bumping `version` in `pyproject.toml` makes the next merge cut a GitHub Release with a matching immutable image tag; roll `CHANGELOG.md` in the same PR.

## License

MIT

---

<sub>MCP Registry identity: `mcp-name: io.github.phdwight/pse-edge-mcp`</sub>
