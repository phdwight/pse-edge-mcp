# CLAUDE.md

MCP server exposing Philippine Stock Exchange (PSE Edge, https://edge.pse.com.ph) data.
Unofficial — Edge has no public API; we speak to the portal's own internal endpoints.

**Read these first — they are the project's memory:**

- `docs/plan.md` — every design decision: scope, architecture, caching policy, auth design, phases, risks. Treat decided items as settled unless the user says otherwise.
- `docs/endpoints.md` — the verified endpoint map (live-captured 2026-07-30): request dialects, param names, response shapes, pagination. Trust this over guesses; PSE Edge param names sometimes differ from its own HTML form fields.

## Non-negotiable invariants

1. **Market-boundary freeze (protect PSE Edge).** This is an EOD-only server. Zero
   upstream requests while the market is open (09:30–15:00 Asia/Manila, trading days).
   All reads go through `FreezeService.get()` — never call `PseEdgeClient` directly
   from a tool. Cache misses during open hours raise `MARKET_OPEN_NO_CACHE`.
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
  `data_policy` is `"EOD-frozen"` normally, or `"immutable"` with `valid_until: null` for
  objects that never change upstream (disclosures by `edge_no`) — pass
  `FreezeService.get(..., immutable=True)` for those. The open-market freeze still applies
  to their first fetch.
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
- **Phase 5 remaining:** default auth on at deploy, usage audit log + retention,
  disposable-email blocklist, CSRF token on /consent, account self-deletion (plan §6a).
- **Phase 6:** production deploy (compose.prod.yaml overlay, TLS, backups).

## Holiday table

`market_calendar.PSE_HOLIDAYS` is a yearly-maintained seed — verify against the PSE
holiday circular each December and when touching calendar logic.
