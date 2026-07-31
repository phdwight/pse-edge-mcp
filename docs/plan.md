# PSE Edge MCP Server — Project Plan

**Working name:** `pse-edge-mcp`
**Goal:** An open-source MCP server that gives any MCP client (Claude Desktop, Claude Code, etc.) structured access to everything the Philippine Stock Exchange discloses through the PSE Edge portal (https://edge.pse.com.ph/).

---

## 1. Key constraint that shapes everything

PSE Edge has **no official public API**. The portal is a JSP web app whose pages are fed by internal JSON/AJAX endpoints (`*.ax`, `*.do`). Community projects (phisix, PSEStockAPI, various scrapers) all work by calling these unofficial endpoints or scraping the HTML.

Consequences for our design:

- **Endpoint discovery is Phase 0.** Before writing server code, we map the actual endpoints with browser dev tools and document them (request shape, params, cookies/session requirements, response schema).
- **Defensive client layer.** Endpoints can change without notice, so all PSE Edge access goes through one isolated client module with schema validation, so breakage is detected loudly and fixed in one place.
- **Polite by design.** Throttling, caching, honest User-Agent, no hammering. We are a guest on their infrastructure.
- **ToS awareness.** Data is public-facing, but we should note in the README that this is an unofficial interface and users consume it at their own risk.

## 2. Scope — data domains (v1)

| Domain | What's exposed | Notes |
|---|---|---|
| Company search | Symbol/name lookup → company id + symbol | Powers everything else |
| Stock quotes | Latest price, open/high/low, volume, value, 52-wk range, market cap, PE, status | Per ticker |
| Price history | Whatever Edge's own chart endpoint provides (OHLC series) | When Postgres is enabled, EOD quotes/disclosures also accumulate into a local archive over time |
| Disclosures | Search by company / date range / template type; disclosure detail | **Metadata + attachment links only** — no PDF download/parsing in v1; the MCP client can fetch PDFs itself |
| Financial reports | Financial highlights from company pages; structured report data where Edge exposes it | Depth limited to what Edge serves as data (not PDF parsing) |
| Indices & market | PSEi + sector indices, daily market summary, gainers/losers/most active | |
| Company info | Profile, sector/subsector, listing date, contact info, dividends/rights notices | "Everything Edge discloses" catch-all |

**Explicitly out of scope for v1:** PDF text extraction, structured statement parsing from PDFs, backfilling deep price history beyond what Edge serves, order/trade data (Edge doesn't have it), real-time streaming.

## 3. MCP tool surface (draft)

Names and shapes to be finalized during Phase 0, but roughly:

- `search_companies(query)` — resolve name/ticker → symbol, company_id, security_id
- `get_stock_quote(symbol)` — latest quote snapshot
- `get_price_history(symbol, period)` — OHLC series from Edge's chart endpoint
- `get_company_profile(symbol)` — profile, sector, listing info
- `get_financial_highlights(symbol)` — key figures Edge exposes as data
- `search_disclosures(symbol?, start_date?, end_date?, template?, page?)` — paginated list
- `search_disclosure_fulltext(keyword, ...)` — search text inside attachments (added in Phase 2; see below)
- `get_disclosure(edge_no)` — full detail + attachment URLs
- `get_dividends_and_rights(symbol)` — dividend/rights notices
- `get_indices()` — PSEi & sector index levels/changes
- `get_market_summary()` — daily summary, gainers/losers/most active

Plus MCP **resources** where it makes sense (e.g., `pse://company/{symbol}` for profile) and a couple of **prompts** (e.g., "summarize today's disclosures for my watchlist") as nice-to-haves.

Design rules: every tool returns typed, validated JSON (Pydantic models → JSON schema in tool definitions); errors are structured (`ENDPOINT_CHANGED`, `SYMBOL_NOT_FOUND`, `RATE_LIMITED`, `EDGE_UNAVAILABLE`) so the client can react sensibly; all timestamps in Asia/Manila with explicit offset.

## 4. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.14** (latest stable, currently 3.14.6) | Latest stable per project policy; free-threading & perf improvements land free |
| MCP framework | Official `mcp` Python SDK (FastMCP API), latest release | Stdio **and** streamable-HTTP transports from the same code — this is what makes "local now, remote later" nearly free |
| Database | **PostgreSQL 18** (latest stable, currently 18.4) | Persistent cache + disclosure/EOD archive; optional in local stdio mode (in-memory fallback), required for remote/scaled deployment |
| DB access | SQLAlchemy 2.x (async) + `asyncpg`; Alembic migrations | Latest stable versions |
| HTTP client | `httpx` (async, latest) | Async, HTTP/2, connection pooling |
| Parsing | `selectolax` or BeautifulSoup for the few HTML-only pages; JSON endpoints preferred wherever they exist | |
| Models | Pydantic v2 (latest) | Validation doubles as endpoint-change detection |
| Retries | `tenacity` | Backoff on transient failures |
| Packaging | `uv` + `pyproject.toml`, console entry point | `uvx pse-edge-mcp` one-liner install |
| Containers | **Docker + Compose v2** (compose spec, `compose.yaml`, no legacy `version:` key) | First-class from Phase 1: multi-stage `Dockerfile` on `python:3.14-slim`, `postgres:18` service with healthcheck; `docker compose up` = full dev stack |
| Auth (HTTP mode) | **OAuth 2.1** per MCP spec — Authlib on the same Starlette app FastMCP runs on; users/clients/tokens in Postgres | Full spec compliance from the start: PKCE, dynamic client registration (RFC 7591), protected-resource metadata (RFC 9728) |
| Email | **ZeptoMail (Zoho)** — decided 2026-07-30 | Verification links only; API key via env at runtime |
| Tests | `pytest` + `respx` (mocked HTTP with recorded real responses); ephemeral Postgres 18 via `testcontainers` for storage tests | CI never hits PSE Edge |
| CI/CD | GitHub Actions: lint (ruff), typecheck (mypy/pyright), tests, publish to PyPI on tag | |

**Version policy:** always target the latest stable release of every dependency at the time of each release; Dependabot/Renovate keeps them current. Docker images pin `python:3.14-slim` and `postgres:18`.

**Image hygiene (required):** images must be thin, optimized, and secret-free —

- *Only what it needs to run (decided; supersedes the earlier "<200 MB" target):* multi-stage build — `uv` and the build toolchain live only in the builder stage; the runtime stage copies just the resolved virtualenv onto bare `python:3.14-slim`. The project installs `--no-editable`, so no source tree or `.pth` indirection ships. No compilers, no uv, no pip, no dev/test dependencies, no bytecode caches. **The CI gate is necessity, not a byte count:** `scripts/check_image.py` asserts the installed distributions equal the runtime closure that `uv export` resolves from the lockfile, so a stray optional extra or a leaked dev dependency fails the build; size is reported for information. A fixed threshold was tried first and rejected — it passed while the image shipped a package manager and 44 MB of unused database drivers, and it would fail on a dependency the app genuinely needs. Corollary: a runtime dependency is added only when code imports it.
- *No bloat:* aggressive `.dockerignore` (`.git`, tests, fixtures, docs, caches, `compose*.yaml`, CI configs); no `pip` cache or `__pycache__` layers (`UV_COMPILE_BYTECODE=0` in the builder, `PYTHONDONTWRITEBYTECODE=1` at runtime — bytecode was ~14 MB of the venv and CPython regenerates it in memory); single-purpose layers ordered for cache reuse (deps before code). Note that deleting base-image files (e.g. pip) in a later layer does *not* reclaim bytes — layers are additive, so that is attack-surface reduction only, and the base image itself is the floor on image size.
- *No secrets:* nothing secret is ever baked into any layer — no `.env` in the build context (dockerignored), no ARG/ENV carrying credentials (build args persist in image history), no tokens in labels. All secrets (`DATABASE_URL`, OAuth signing keys, email API key) arrive at **runtime only** via Compose `env_file`/environment or Docker secrets. If a build step ever needs a private credential, use BuildKit `--mount=type=secret` (never copied into a layer). CI runs a secret-scan (e.g. trivy/gitleaks) over the built image and fails on findings.
- Runtime runs as a non-root user with a read-only filesystem where practical.

**Container layout (Compose v2):** `compose.yaml` defines two services — `app` (multi-stage build: uv dependency layer → slim runtime, non-root user, runs `pse-edge-mcp --http`) and `db` (`postgres:18` with named volume, healthcheck via `pg_isready`, `app` waits on `depends_on: condition: service_healthy`). Compose **profiles** keep the DB optional: plain `docker compose up` runs the full stack, while stdio/PyPI users never need Docker at all. `.env` file carries `DATABASE_URL` and secrets; `compose.prod.yaml` overlay adds production settings (Phase 6).

## 5. Architecture — built to scale later

```
┌─────────────────────────────────────────────┐
│  MCP layer (FastMCP)                        │
│  tools / resources / prompts                │
│  transport: stdio (now) │ streamable HTTP   │
├─────────────────────────────────────────────┤
│  Auth & accounts (HTTP transport only)      │
│  OAuth 2.1 (Authlib) · signup + email verify│
│  per-user tiered quotas · admin ops         │
│  — bypassed entirely in stdio mode —        │
├─────────────────────────────────────────────┤
│  Service layer                              │
│  orchestration, caching policy, pagination  │
├─────────────────────────────────────────────┤
│  PseEdgeClient  (pure, MCP-agnostic)        │
│  endpoints, session/cookie handling,        │
│  throttle, retries, Pydantic validation     │
├──────────────┬──────────────────────────────┤
│ StorageBackend (Protocol)  │  RateLimiter   │
│ InMemoryTTL (stdio default)│  (token bucket │
│ PostgreSQL 18 (opt-in now, │   + single-    │
│  default for remote mode)  │   flight dedup)│
└──────────────┴──────────────────────────────┘
```

Scalability decisions made **now**, paid for **later**:

1. **Storage is a Protocol** (`get/set` + archive queries) — two implementations: `InMemoryCache` (zero-config stdio default) and `PostgresStorage` (Postgres 18 via async SQLAlchemy, enabled with `DATABASE_URL`). Expiry follows the **market-boundary freeze policy** (see §5a), not wall-clock TTLs.
2. **Postgres doubles as an archive** — when enabled, disclosures and daily EOD quotes are upserted as they pass through, so historical depth accumulates for free over time (Edge itself offers limited history). Schema managed with Alembic from day one.
3. **Stateless server** — all state lives in the storage backend, so horizontal scaling is trivial once storage is external.
4. **Transport is a runtime flag** — `pse-edge-mcp` (stdio, default) vs `pse-edge-mcp --http --port 8000`. Same tools, zero code changes.
   **HTTP is stateless with plain JSON responses by default (decided).** The server declares no
   `listChanged`, no `subscribe`, and uses no sampling, elicitation or progress, so MCP sessions
   would buy nothing while costing sticky routing, per-session memory and an event store. Verified:
   a single bare POST with no `initialize` and no `Mcp-Session-Id` returns the full tool list and
   executes tool calls; under `--stateful` the same request is correctly rejected with 400. This is
   what makes horizontal scaling ordinary, and it is the last piece of "any replica, any request"
   after Phase 4 moved shared state into Postgres. `--stateful` / `--sse` restore session mode and
   SSE framing for anyone who needs resumability or server-initiated messages.
5. **`PseEdgeClient` is importable on its own** — usable as a plain Python library, and independently testable.
6. **Single-flight request dedup** — concurrent identical requests (common when an LLM fans out tool calls) collapse into one upstream hit.

## 5a. Caching policy: market-boundary freeze (decided)

**Purpose: protect PSE Edge.** The server is deliberately an **EOD data service** — upstream queries happen around market-closed times, and everything in between is served from the shared cache. Intraday freshness is a non-goal.

- **Boundaries, not TTLs.** A cached value's validity runs to the next PSE session boundary (Asia/Manila): market open (~9:30 AM) and market close (3:00 PM) on trading days. Example: AREIT fetched Monday pre-open serves every query — through the whole trading session — until Monday 3:00 PM; the first query at 3:01 PM triggers one fresh fetch (capturing that day's final numbers), which then serves until the next boundary.
- **One fetch per boundary crossing, per cache key.** The first query after a boundary refetches; single-flight dedup guarantees concurrent first-queries collapse into one upstream hit. All users share the result (Postgres-backed cache in remote mode).
- **Cache miss during open hours: refuse.** No upstream fetch happens while the market is open, ever. The tool returns a structured `MARKET_OPEN_NO_CACHE` error with a human-readable message ("no cached data for this symbol; try again after the market closes at 3:00 PM Asia/Manila") plus a `retry_after` timestamp set to the close boundary — so LLM clients can relay it clearly or even schedule a retry. Guarantees zero intraday traffic to PSE Edge under all circumstances.
- **Uniform scope: ALL data domains follow this policy** — quotes, OHLC, indices, disclosures, financials, profiles. Consequence (accepted): a disclosure filed at 10 AM becomes visible after the 3 PM close. Immutable objects (disclosure details/attachments by `edge_no`) never refetch at all.
- **Calendar source:** open/close times in config + a maintained PSE holiday table (Postgres table with yearly seed file). Weekends/holidays have no boundaries — cache simply persists, zero upstream traffic.
- **Honesty in responses:** every tool result carries `as_of` (fetch timestamp), `valid_until` (next boundary), and a `data_policy: "EOD-frozen"` marker so LLM clients can tell users exactly how fresh the data is.
- The global outbound politeness throttle stays as a second, independent layer beneath this.

## 6. Users, auth & abuse prevention (HTTP mode)

**Principle:** local stdio mode stays auth-free (it runs on the user's own machine and hits PSE Edge directly — registration there adds friction without preventing abuse). The remote HTTP server requires an account.

- **Auth standard: full OAuth 2.1**, as specified by the MCP authorization spec — PKCE required, dynamic client registration (RFC 7591) so MCP clients can self-register, protected-resource metadata (RFC 9728) for discovery, short-lived access tokens + refresh tokens. This makes connecting from Claude.ai custom connectors "paste URL → authorize → done".
- **Token strategy (decided; rationale revised 2026-07-30):** every HTTP request carries `Authorization: Bearer <access_token>` and is authenticated independently — the MCP streamable-HTTP session ID is never used for auth (per spec; moot anyway since HTTP mode is stateless by default). Access tokens are **opaque** (random, full-entropy, hashed at rest with SHA-256 — no slow hash needed since these are not low-entropy passwords) with **~30-minute TTL**, paired with refresh tokens; clients refresh silently in the background. Validation is one indexed Postgres lookup fronted by an in-process cache whose **TTL is the revocation-latency budget and nothing else** (default 60 s, configurable). The maths that killed the original "~5 s" figure: `auth lookups/s ≈ min(request rate, active tokens ÷ cache TTL)` — on an EOD service where few users make more than one request per few seconds, a 5 s cache saves almost nothing, while 60 s cuts auth reads ~6× and caps revocation lag at one minute. Cached validity never outlives the token itself.
  The original argument for opaque-over-JWT ("the quota check requires a DB hit per request anyway") is **retired** — quotas no longer touch the database per request (next bullet). Opaque tokens stay decided on the merits that remain: instant revocation (bounded by the cache TTL) is worth more to an abuse-prevention design than a JWT signature check, and after the 0.4.0 parse memo a cache-hit request costs ~0 ms of CPU, making auth the most expensive step on the hot path *either way*. Revisit JWTs only if the auth server is ever split from the MCP server.
- **Implementation (revised 2026-07-30 at the scheduled revisit): Authlib dropped; the OAuth server is implemented directly.** Verified empirically at Phase 5 kickoff: Authlib 1.7 ships OAuth *server* integrations for Flask and Django only — `authlib.integrations.starlette_client` is the **client** side. Adopting it would have meant adapting a Flask-shaped authorization server onto Starlette for a surface that is small and fully enumerable: public clients only, authorization code with mandatory S256 PKCE, refresh rotation. `oauth.py` implements exactly that, with every security rule stated in its docstring and pinned by a test in `tests/test_oauth.py` (open-redirector refusal, exact redirect matching, PKCE mandatory/S256-only, single-use codes via one atomic UPDATE, refresh-reuse family revocation). Users, clients, flows and token hashes live in Postgres 18. Keycloak/hosted-provider remains the fallback if the surface ever grows beyond this.
- **Registration & login: passkeys (WebAuthn/FIDO2) — no passwords, ever.** Signup: email → verification link → **passkey enrollment** (WebAuthn ceremony) → account active. Login on the OAuth authorization page is a passkey ceremony. Implementation: `py_webauthn` for the WebAuthn server side, wired into the Authlib authorization server's login/signup UI. **Email remains the account identifier and recovery path** — losing a passkey means re-verifying email to enroll a new one — and keeps the disposable-email abuse brake. **Multiple passkeys per account** (add/remove from an account settings page) so users can enroll laptop + phone; credential public keys, sign counts, and AAGUIDs stored in Postgres alongside the user. Phishing-resistant by design, and a WebAuthn ceremony is meaningfully harder to bot than a form signup. Transactional email: **ZeptoMail (Zoho)** — decided 2026-07-30; the operator already runs it. Plain HTTPS POST to `https://api.zeptomail.com/v1.1/email` with a `Zoho-enczapikey` authorization header; the key arrives at runtime via environment only (never in the repo or image, per the no-secrets rule).
- **Quotas: enforced in-process, never as a per-request database write (revised 2026-07-30).** Limits are stored per-user in Postgres (default 60 req/min, 2,000 req/day; nullable per-user overrides let the admin raise them), but *counting* happens in per-replica in-memory windows — a counter `UPDATE` per request is hot-row lock contention plus WAL churn, the same class of defect as the archive-on-cache-hit bug fixed in 0.4.0. Consequence, accepted: with N replicas a user's effective ceiling is up to N× the nominal limit. Abuse prevention needs to stop the abuser, not bill exactly, so approximate is correct here; periodic flush to a usage log arrives with the audit-log work. Enforced in middleware before any PSE Edge traffic; over-limit returns HTTP 429 with `Retry-After` and a structured `RATE_LIMITED` body. The server's *outbound* politeness throttle toward PSE Edge remains a separate, global layer — user quotas protect *your* service, the outbound throttle protects *theirs*.
- **Admin operations:** small CLI (and/or protected endpoints) for listing users, changing tiers, revoking tokens/accounts, viewing per-user usage counters.
- **Security hygiene:** tokens hashed at rest, per-user usage audit log (also feeds future analytics), disposable-email domain blocklist at signup as a cheap abuse brake.

## 6a. Operations, compliance & lifecycle (decided)

- **Observability:** structured JSON logs throughout; a **nightly canary** (scheduled, runs during closed hours) hits one real endpoint per family and validates against the Pydantic schemas; canary failure or a spike in runtime schema-validation errors triggers an **email alert** to the operator. No metrics stack in v1 (revisit if remote usage grows).
- **Archive mode: opportunistic only.** The EOD archive grows solely from user-triggered fetches — zero extra load on PSE Edge. A full nightly sync of all listed symbols is explicitly deferred; can be added later as an opt-in job if archive gaps hurt.
- **Privacy compliance (registration data) — delivered 2026-07-31.** Collecting emails makes the operator a personal-information controller under the PH Data Privacy Act (and GDPR for foreign users). All five requirements are implemented: `/privacy` policy page (public — a policy you must sign up to read is not a policy); `/account` subject-access view plus a self-service `POST /account/delete` that erases immediately with no approval step; email as the only identifying field collected; usage retention capped at **90 days**, purged automatically; breach-notification contact stated in the policy.
  Decisions taken while building it:
  1. **The usage log aggregates per user-hour, not per request.** It answers what a log exists for — what did this account do, roughly when — while holding markedly less about a person, which is itself the §6a "minimal collection" requirement rather than a shortcut around it. It also keeps writes off the request path (the quota rule) and makes retention an indexed range delete. Cost, accepted: a crash loses at most one flush interval; that is fine for an abuse-and-transparency log and would not be for billing.
  2. **Erasure is a hard delete in one transaction**, never a `disabled_at` flag — a soft delete leaves the email on file, which is the opposite of erasure and would make the privacy page's promise false. Public market data (`eod_bars`, `disclosures`) survives because it was never about the user. The completeness test walks `metadata.tables` rather than a hand-written list, so a table added later that references a user fails the test instead of silently retaining personal data.
  3. **The operator's `delete-user` and the user's own deletion share one code path**, so the operator route cannot drift from the promise made to users.
  4. CSRF tokens on `/consent` and `/account/delete`, derived from the session id rather than stored. SameSite=Lax already blocks the cross-site POST; this is defence in depth.
  5. Disposable-email domains are refused at signup (plan §6's abuse brake) — kept a small readable list, since a vendored 100k-domain file would imply a completeness it cannot have and would rot silently.
- **Backups:** scheduled `pg_dump` (daily, during closed hours) with rotation/retention in the prod Compose overlay; the named volume plus dumps cover the two unrecoverable assets — user accounts and the accumulated archive.
- **Releases:** semver + `CHANGELOG.md` (Keep a Changelog format); `v0.x` during Phases 1–4; **v1.0 = the remote server with OAuth is live and stable**; PyPI publish and image build on git tag via CI.

## 7. Delivery phases

- **Phase 0 — Recon (no server code):** map every useful Edge endpoint in the browser; record real request/response samples into `docs/endpoints.md` + test fixtures. Exit criteria: documented endpoint list covering all v1 domains, incl. session/cookie quirks.
- **Phase 1 — Skeleton + quotes:** repo scaffold, CI, **Dockerfile + `compose.yaml`** (app + `postgres:18` with healthcheck; `docker compose up` brings up the full dev stack, `docker compose --profile db down` etc. via Compose v2 profiles so stdio-only use needs no DB), `PseEdgeClient`, cache/ratelimit, FastMCP wiring; `search_companies`, `get_stock_quote`, `get_price_history`. Works in Claude Desktop via stdio.
- **Phase 2 — Disclosures (delivered):** search + detail + attachment links, pagination.
  Two deviations from the original design, both forced by what the fixture pass found
  (docs/endpoints.md v3):
  1. The market-wide search backend is `/announcements/search.ax`, not
     `/keyword/search.ax`. The latter searches *attachment text* against an index that is
     partial and stale (measured: 2023-2025 only, nothing from 2026), so it could not
     serve "recent disclosures". It survives as a separate tool,
     `search_disclosure_fulltext`, which reports its own coverage limits in-band so an
     LLM client relays "Edge's index doesn't cover this period" instead of "no disclosures".
  2. Pagination is exact, not blind. Every `search.ax` response carries `[Total n]` and
     `[page / pages]`, so tools return `total`/`pages`/`has_more` and take a `page`
     argument rather than looping until a short page — fewer upstream requests for the
     same information, which the freeze policy's whole purpose recommends.
- **Phase 3 — Company & market (delivered):** profile, financial highlights, dividends/rights, indices, market summary.
  Scope notes from the fixture pass (docs/endpoints.md v4):
  1. **Financial figures are never rescaled.** Edge's own units labels contradict each other
     — the annual section said "Php (in thousands)" while quarterly said "Php (in Millions)"
     for the same company, with one identical figure appearing under both. Each period
     reports its `currency_units` verbatim and the tool docstring tells the model to check it
     before quoting a number. Normalising would have encoded Edge's error as fact.
  2. **Gainers/losers/most-active stay out**, as decided in Phase 0 — Edge publishes them
     nowhere. `get_market_summary` says so rather than implying the data is merely missing.
  3. `get_market_summary` keys its feeds by Edge's own group labels instead of invented
     buckets, so a renamed or added group surfaces instead of being dropped.
  4. `directors_and_management_list.do` is mapped and trivial to parse but exposes no tool —
     it is not in the v1 surface in §3. Add when wanted.
- **Phase 4 — Storage & archive (delivered):** Postgres storage backend + Alembic schema + archive upserts.
  Decisions taken during the build:
  1. **Postgres stays genuinely optional.** The driver modules are imported lazily inside
     `build_storage()`, so `pip install pse-edge-mcp` without the `postgres` extra still runs
     in stdio mode. A test asserts SQLAlchemy does not leak into `sys.modules` when the server
     is imported. The *image* does install the extra, since compose runs it against Postgres.
  2. **No `create_all` at runtime.** Schema comes from Alembic alone; compose applies it with a
     one-shot `migrate` service gated on `service_completed_successfully`, and `check_schema()`
     fails at startup with an actionable message if migrations were skipped. Replicas must not
     race to mutate their own schema.
  3. **`cache_entries` has no TTL column**, by design — freshness stays the calendar's decision
     (§5a). A test asserts the column set, so a future expiry column cannot quietly introduce a
     competing policy.
  4. **A failed archive write never fails the user's read.** Deliberately broad `except`: a
     database that is down raises `OSError`/`ConnectionRefusedError` from the driver socket, not
     `SQLAlchemyError`, so catching only the latter would have broken every price-history read
     during an outage. A test covers it.
  5. Storage tests run against a real ephemeral **Postgres 18** via testcontainers and apply the
     **real migration** rather than `metadata.create_all`, so migration drift is caught here
     instead of in production. They skip cleanly without Docker.
  Deferred to a later phase: README install polish, PyPI publish.
- **Phase 5 — Accounts & OAuth 2.1 (staged; stage 1 in progress):**
  Stage 1 — enforcement first: users + hashed-token schema, bearer validation with the
  revocation-budget cache, in-process quotas, admin CLI for manual provisioning
  (create-user / issue-token / revoke / set-limits). Opt-in via `PSE_AUTH_REQUIRED=1`
  until the self-service flow exists, so current deployments don't break; requires
  `DATABASE_URL` (accounts live in Postgres; stdio stays auth-free).
  Stage 2 — the OAuth surface: Authlib authorization server (PKCE, dynamic client registration, resource metadata), signup with email verification + **passkey enrollment (py_webauthn)**, passkey login ceremony on the authorization page, multi-passkey management page, user/credential/token schema in Postgres, tiered quota middleware, admin CLI. Exit criteria: a fresh user can sign up with a passkey, connect from Claude.ai custom connectors via the standard OAuth flow, log in from a second device, and hit their rate limit cleanly.
- **Phase 6 — Remote deploy (delivered):** `compose.prod.yaml` overlay + `Caddyfile` + `docs/deploy.md`. Caddy terminates TLS with automatic ACME; `app` and `db` publish no ports at all, so everything arrives encrypted through the proxy. Auth is **on by default in production** — anonymous access to a public deployment should not be something you opt out of. Daily `pg_dump` at 02:00 Asia/Manila (after the close, away from the boundary refetch) with rotation that runs only after a *successful* dump, plus a daily usage-retention purge.
  Decisions taken while building it:
  1. **An importable ASGI app (`asgi.py`), not just the CLI path.** uvicorn's multi-worker supervisor forks and re-imports, so it needs an import string — an object built inside `main()` cannot be shared with children. The factory also lets gunicorn or any other ASGI server run this, and means `__main__` and production compose the *same* stack rather than two that drift. Resolved lazily via PEP 562 `__getattr__`, so importing the module opens no connections and raises no SystemExit.
  2. **Liveness and readiness are separate endpoints.** `/health` is cheap and dependency-free; `/health/ready` checks the database and answers 503. A liveness probe that touches Postgres restarts every replica during a blip — a recoverable outage becomes an outage plus a restart storm. The container healthcheck uses liveness only.
  3. **Structured JSON logs are stdlib-only** (`PSE_LOG_JSON=1`), with secret redaction as a backstop. uvicorn's handlers are re-pointed at ours so output is not half JSON and half prose. `configure_logging` replaces only handlers it installed itself — clearing every root handler would be idempotent by stomping on a host application's logging.
  4. **Per-worker state is documented, not hidden.** Quota windows, parse memo and token cache are per process, so N workers means a user's ceiling is up to N× nominal. The overlay uses 2 as a compromise; `docs/deploy.md` says to scale limits with workers.

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Edge changes/removes endpoints | Isolated client + Pydantic validation → loud, localized failure; recorded fixtures make fixes fast |
| Session/cookie requirements (JSESSIONID etc.) | Client bootstraps a session like a browser would; documented in Phase 0 |
| Rate limiting / IP blocking | Token-bucket throttle, caching, single-flight, honest UA, backoff |
| Stale data misleading users | Every response carries `as_of` timestamp + `from_cache` flag |
| ToS / legal gray area | README disclaimer: unofficial, personal/research use, no warranty |
| Self-hosted OAuth server complexity/security | Authlib (battle-tested) rather than hand-rolled; tokens hashed; scope kept minimal; Keycloak/hosted-provider fallback documented |
| Signup abuse (throwaway accounts) | Email verification + disposable-domain blocklist + low free tier; admin revocation |
| Email deliverability | Use an established transactional provider; verification links are the only email we send |

## 9. Open items (fine to settle during Phase 0)

- Final package/repo name (`pse-edge-mcp` placeholder — check PyPI availability)
- Exact disclosure template taxonomy Edge uses (drives `search_disclosures` filters)
- Whether financial highlights are served as JSON or need HTML parsing (affects Phase 3 effort)
- License (MIT suggested)
- Free-tier quota numbers (60 req/min / 2k per day is a starting guess)
