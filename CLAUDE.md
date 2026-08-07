# CLAUDE.md

MCP server exposing Philippine Stock Exchange (PSE Edge, https://edge.pse.com.ph) data.
Unofficial — Edge has no public API; we speak to the portal's own internal endpoints.

**Read these first — they are the project's memory:**

- `docs/plan.md` — every design decision: scope, architecture, caching policy, auth design, phases, risks. Treat decided items as settled unless the user says otherwise.
- `docs/endpoints.md` — the verified endpoint map (live-captured 2026-07-30): request dialects, param names, response shapes, pagination. Trust this over guesses; PSE Edge param names sometimes differ from its own HTML form fields.

## Non-negotiable invariants

1. **Market-boundary freeze — prices only (narrowed 2026-08-07, 0.13.0).** Stock quote
   and price history are EOD-only: zero upstream **price** requests while the market is
   open (09:30–15:00 Asia/Manila, trading days); a price cache miss during open hours
   raises `MARKET_OPEN_NO_CACHE`. Every other domain is fetch-once-then-persist: a miss
   may hit PSE Edge at *any* hour — once, single-flighted — and repeats of the same query
   are served from storage until the next close (`data_policy: "daily-refresh"`). All
   reads still go through `FreezeService.get(..., policy=...)` — never call
   `PseEdgeClient` directly from a tool. The default policy is the strictest
   (`EOD-frozen`), so an unlabelled read can only over-protect PSE Edge.
2. **Tests never touch PSE Edge.** All HTTP is mocked with respx against fixtures in
   `tests/fixtures/` (recorded from real captures). New endpoints need new fixtures.
3. **Two request dialects** (see docs/endpoints.md §0): JSON-body POST for chart-style
   `.ax` endpoints (form-encoded gets HTTP 415); form-encoded POST returning HTML
   fragments for `search.ax` endpoints. Wire dates are `MM-dd-yyyy`.
4. **Loud on drift.** If Edge's response shape changes, raise `EndpointChangedError` —
   never silently return partial data.
5a. **Postgres is optional, and the image declares it.** The runtime image installs
   `--extra postgres` (compose runs it against Postgres and code uses the driver), so the
   image check must be passed `--extra postgres` or the drivers look like bloat. The
   library install stays thin via lazy imports.
5. **The runtime image contains only what the app needs to run** — and no secrets.
   Multi-stage, non-root, no build tools or package manager in runtime, no dev deps, no
   bytecode caches, no source tree (the project installs `--no-editable`), no credentials
   in any layer/ARG/ENV. **The gate is necessity, not a size number:**
   `scripts/check_image.py` asserts installed distributions equal the resolved runtime
   closure from `uv export` (so any stray extra or leaked dev dep fails), and reports size
   as information only. Don't reintroduce a megabyte threshold — it passes while shipping
   junk and fails on a genuinely needed large dependency. Add a runtime dependency only
   when code imports it (this is why the `postgres` extra waits for Phase 4).

## Branching & release (decided)

- **All work lands on `develop`.** No feature branches. `develop` reaches `main` only by
  pull request; `main` is protected (PR required, `test` + `image (amd64)` + `image (arm64)`
  checks must pass, no force pushes or deletions, admins exempt for hotfixes). Never
  commit to `main` directly.
- **Merging to `main` publishes a multi-arch image** (`linux/amd64` + `linux/arm64`) to
  `ghcr.io/phdwight/pse-edge-mcp` (`:latest`, `:sha-<sha>`) via
  `.github/workflows/release.yml`. Each arch builds on its own **native runner** (free on
  public repos, no QEMU) and is size-checked, secret-scanned, and smoke-tested *before*
  publish, so invariant #5 holds per architecture. Publishing is two-phase: each arch
  pushes by digest, then a merge job stitches the digests into one manifest list under the
  real tags, so a tag never exists half-published. If you add a platform, update the
  digest count assertion in the merge job **and** the required-check contexts.
- **Releases are version-driven:** a merge that changes `version` in `pyproject.toml` also
  cuts a GitHub Release and an immutable `:<version>` tag. Merges without a bump only move
  `:latest`. So bump the version and roll `CHANGELOG.md`'s Unreleased section in the same
  PR as the work being released.
- `ci.yml` covers PRs and `develop` pushes; `release.yml` covers `main`. Don't add `main`
  back to `ci.yml` — it would double-build the image.

## Conventions

- Python 3.14, `uv` for everything (`uv sync --all-extras`, `uv run pytest`, `uv run ruff check .`).
- ruff line-length 100; mypy strict; pytest-asyncio in auto mode.
- Compose v2 (`compose.yaml`, no `version:` key). `docker compose up` = app + Postgres 18.
- Every tool result returns `{"data": ..., "meta": {as_of, valid_until, from_cache, stale, data_policy}}`.
  `data_policy` (a per-read `policy=` on `FreezeService.get`) is `"EOD-frozen"` for price
  data (market-gated), `"daily-refresh"` for every other domain (fetched at any hour, at
  most once per boundary window), or `"immutable"` with `valid_until: null` for objects
  that never change upstream (disclosures by `edge_no`). `stale` means real data past its
  boundary: the market is open (price data) or PSE Edge was unreachable (anything).
- Timestamps ISO-8601 in Asia/Manila. Accounting negatives `(1,234)` → `-1234`.
- **Layering (revised — a domain layer was added; keep it that way):**

  ```
  server.py        MCP boundary only: validate args, delegate, shape the reply.
                   No domain logic, no cache keys, no parsing, no endpoint choices.
    ↓
  repositories.py  One repository per data domain. Owns cache key + freeze read +
                   parse + model for that domain. Endpoint routing lives HERE.
    ↓
  service.py       Freeze policy (FreezeService). Repositories depend on the
  sources.py       `FrozenCache` protocol, and on the narrow per-domain source
                   protocols — never on the concrete client or service.
    ↓
  client.py        Pure HTTP, MCP-agnostic.  parsers.py  HTML/JSON → dicts.
  ```

  Supporting modules: `validation.py` (arg validators raising `INVALID_ARGUMENT`),
  `models.py` (Pydantic), `errors.py`, `cache.py` (`Storage` protocol), `ratelimit.py`,
  `market_calendar.py`, `config.py`.

  Rules that follow from it:
  - A new data domain (Phase 3: financials, dividends, indices) = a new repository +
    thin tools. Don't add fetch/parse orchestration to `server.py`.
  - Tools never build cache keys or call `parse_*` — that's the repository's job.
  - Repositories take the narrow protocols from `sources.py`, so they're testable with
    a few-line fake and no HTTP mocking (see `tests/test_repositories.py`).
  - Error mapping happens once in `server.reply()`; don't add per-tool try/except.
  - `reply()` is a helper taking a callable, deliberately **not** a decorator: the MCP
    SDK builds each tool's output schema from its return annotation, and a
    `functools.wraps` wrapper makes `inspect.signature` follow `__wrapped__` to the
    inner annotation, which breaks tool-schema generation. Tools must stay annotated
    `-> dict[str, Any]`. `tests/test_server_disclosures.py::test_tool_surface_is_stable`
    guards this.

## Roadmap (details in docs/plan.md §7)

- **Phase 2 (done):** disclosures — `search_disclosures` (`/announcements/search.ax`
  market-wide + date range, `/companyDisclosures/search.ax` for full per-company history),
  `search_disclosure_fulltext` (`/keyword/search.ax`, attachment text; index only covers
  ~2023-2025, so it is a separate tool with a coverage warning, not the primary search),
  `get_disclosure(edge_no)` via `openDiscViewer.do`. Disclosure tables are parsed by
  `<thead>` label, never by column position — the two endpoints order columns differently.
  See docs/endpoints.md §"v3 corrections" for the three v2 claims this phase disproved.
- **Phase 3 (done):** `get_company_profile`, `get_financial_highlights`,
  `get_dividends_and_rights`, `get_indices`, `get_market_summary`. Findings that shape the
  code (details in docs/endpoints.md v4): financial-report **units labels contradict each
  other** between the annual and quarterly sections, so values are passed through verbatim
  and never rescaled; index `Chg`/`%Chg` are printed **unsigned** with direction only in a
  colour and a ▲/▼ glyph, so signs are derived from the glyph; `dividends_and_rights_form.do`
  is an empty shell that posts to `dividends_and_rights_list.ax` once per tab, with
  `DividendsOrRights` in the query string and `cmpy_id` in the body; homepage feeds are keyed
  by Edge's own group labels rather than invented buckets. `directors_and_management_list.do`
  is mapped but has no tool yet.
- **Phase 4 (done):** Postgres storage backend (`storage_postgres.py`, same `Storage`
  protocol) + Alembic (`migrations/`) + opportunistic archive (`archive.py` protocol,
  `archive_postgres.py` impl). `DATABASE_URL` unset = in-memory cache + `NullArchive`;
  set = shared cache + archive. Key rules: **Postgres stays optional** — `db.py`,
  `storage_postgres.py` and `archive_postgres.py` are imported lazily inside
  `build_storage()`, so a plain install without the `postgres` extra never hits an
  ImportError (a test asserts sqlalchemy does not leak into `sys.modules` on
  `import pse_edge_mcp.server`). **Schema is applied by Alembic only**, never `create_all`
  at runtime — compose has a one-shot `migrate` service and `check_schema()` fails loudly
  at startup if migrations were skipped. **A failed archive write never fails a read**
  (caught broadly: a dead database raises OSError, not just SQLAlchemyError).
- **Phase 5 (staged; stage 1 done):** bearer auth + quotas + admin CLI, opt-in via
  `PSE_AUTH_REQUIRED=1` (needs `DATABASE_URL`; stdio never authenticates). Key rules:
  tokens are opaque, `pse_`-prefixed, stored as SHA-256 only; the validation-cache TTL
  **is the revocation-latency budget and nothing else** (default 60 s); refusals are never
  cached; quotas count **in-process** (per-request counter UPDATEs are hot-row contention
  — plan §6 records why the old "DB hit per request anyway" JWT argument is retired);
  `auth.py` must stay SQLAlchemy-free (lean-install path), Postgres bits live in
  `auth_store.py` and import lazily. Provisioning: `pse-edge-admin`.
- **Phase 5 stage 2 (done):** OAuth 2.1 + passkeys. `oauth.py` (DCR/authorize/PKCE/refresh),
  `passkeys.py` (WebAuthn + web sessions), `auth_app.py` (pure-ASGI route table wrapping the
  guarded MCP app), `email.py` (ZeptoMail | console). **Authlib was dropped** — it has no
  server-side ASGI integration (Flask/Django only); see plan §6. Rules that must not regress:
  unknown client/redirect is fatal and never redirects (open-redirector); redirect URIs match
  exactly; PKCE mandatory + S256 only; codes single-use via one atomic UPDATE...RETURNING;
  refresh reuse revokes the whole family; only `kind='access'` authenticates as a bearer.
  Layering is `AuthApp(AuthMiddleware(mcp_app))` so `/oauth/*` and signup are reachable
  without a token. Journey test: `tests/test_auth_journey.py` (soft-webauthn, real
  signatures, real Postgres).
- **Phase 5 privacy (done):** `/privacy` page, `/account` subject-access view, self-service
  `POST /account/delete`, usage log with 90-day retention, disposable-email blocklist, CSRF
  on both state-changing forms. Rules: the usage log **aggregates per user-hour, never per
  request** (minimal collection *and* no hot-path write); erasure is a **hard delete in one
  transaction**, and `tests/test_privacy.py::test_erasure_leaves_nothing_behind` walks
  `metadata.tables` — a new user-keyed table fails that test rather than silently retaining
  data; the admin `delete-user` reuses the user's own erasure path so the two cannot drift.
  `usage.py` must stay SQLAlchemy-free; the sink lives in `usage_postgres.py`.
- **Phase 5 stage 3 (done, 0.8.0):** `client_credentials` for headless agents. **The gate is
  the whole feature:** `/oauth/register` is open, so authorization for this grant must never
  be derivable from anything a registrant supplies (a self-declared `grant_types`, a
  requested auth method, a presented secret). It is `oauth_clients.client_type == 'machine'`,
  written **only** by `pse-edge-admin create-machine-client`; a DCR client gets
  `unauthorized_client` regardless of what it sends, and the type check runs *before* any
  secret comparison so the endpoint is not an oracle. Other rules: a machine client is backed
  by a **service user** (`*@machine.invalid`) so the bearer path stays identical for both
  grants and quotas/usage/disablement come for free; secrets are 48 bytes, stored SHA-256
  only, compared in constant time; **no refresh token** for this grant; unknown client and
  wrong secret answer identically; `POST /oauth/token` is rate-limited per client_id *and*
  per IP (`FixedWindowLimiter`). Metadata advertises the grant — advertising is not
  authorization.
- **Logging (0.8.1):** both formatters timestamp every line (ISO-8601 + offset) and both
  redact. INFO on the critical paths, **refusals only** — a success is already an access-log
  line. `upstream: fetching from PSE Edge key=... policy=...` is the line that matters:
  since 0.13.0 it carries the read's policy, and a **`policy=EOD-frozen` fetch during
  market hours means the freeze invariant is broken** (other policies legitimately fetch
  at any hour, at most once per key per boundary window).
  WARNING is reserved for refresh-token reuse and a non-machine client
  denied `client_credentials`. A startup line states the resolved config (presence, never
  values).
- **Machine clients are provisionable from `/account`, operator-gated (0.11.0).** The web
  route creates/revokes machine clients for accounts whose email is in `PSE_ADMIN_EMAILS`,
  so a NAS operator with no shell can grant headless agent access. **This must not widen who
  may create machine clients:** the `client_credentials` gate depends on them being
  admin-only, and the allowlist is that admin identity over HTTP. A non-admin sees no panel
  and the routes answer 404; CSRF-guarded; secret shown once. `admin_emails` is never
  populated from anything a user sets about themselves.
- **`send_email` (0.9.0) — the first and only action tool.** Rules that must not regress:
  **the recipient is never an argument**, it comes from the validated bearer token via the
  ASGI scope (`server._caller`). Signup is open, so a `to` parameter would make this an
  internet-facing mail relay, and disclosure text from PSE Edge is untrusted content a model
  reads — an address argument is an exfiltration path for prompt injection. Also: registered
  **only when `auth_required`** (no verified address otherwise, and an always-failing tool
  makes a model keep choosing it); body is **escaped, never rendered as HTML**; caps 200 /
  20,000 chars and 20 per user per day; policy lives in `notifications.py`, not `server.py`.
  Action tools return `{"data": …}` with **no `meta`** via `act()` — `meta` is a *freshness*
  contract and an action has no `as_of`.
- **Schema canary (0.10.0) — plan §6a delivered.** `canary.py` + `pse-edge-canary` + a
  nightly compose service. Rules: it **bypasses the cache** (a warm entry would validate
  yesterday's HTML), **still refuses to run while the market is open** (invariant #1
  outranks it), and **validates the Pydantic model, not the HTTP status** — a 200 with a
  restyled table is the failure it exists for. Emails `PSE_OPERATOR_EMAIL` **only on
  failure**; a nightly "all fine" gets filtered and then feels like coverage. Each check
  must mirror how the *repository* builds its model, or the canary reports drift the tools
  never see. Exits non-zero so cron/CI can notice.
- **Upstream outage = stale, not error (0.10.0).** If a fetch fails with
  `EdgeUnavailableError` and an expired entry exists, `FreezeService` serves it flagged
  `stale` instead of raising. Discarding real data to return an error is strictly worse, and
  `stale` already means "real data, past its boundary", so clients need no change.
  `EDGE_UNAVAILABLE` now means unreachable **and nothing cached**.
- **Phase 5 remaining:** flip auth to default-on at deploy.
- **Phase 6 (done):** `compose.prod.yaml` + `Caddyfile` + `docs/deploy.md`. Rules:
  the HTTP stack is composed **once** in `asgi.py` — `__main__` and production must not
  build it separately; `asgi.app` resolves lazily (PEP 562) so importing the module has no
  side effects; `/health` is liveness (never touches the DB — a DB-dependent liveness probe
  restarts every replica during a blip) and `/health/ready` is readiness; `configure_logging`
  removes only handlers it installed. **`PSE_PUBLIC_URL` must be the real external https URL**
  — it drives the WebAuthn rp_id, email links and OAuth issuer, and a wrong value breaks
  passkeys in a way that looks like a browser bug. Workers are processes: all in-memory
  state is per worker, so quotas loosen by a factor of N.

## Holiday table

`market_calendar.PSE_HOLIDAYS` is a yearly-maintained seed — verify against the PSE
holiday circular each December and when touching calendar logic.
