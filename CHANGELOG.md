# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/) · Versioning: [SemVer](https://semver.org/).


## [Unreleased]

### Fixed
- **Archiving ran on cache hits.** `search_disclosures` and `get_price_history` recorded to the archive unconditionally, so a request served entirely from cache still wrote up to 50 `ON CONFLICT DO NOTHING` rows. At 1,000 req/s that is ~50k no-op upserts per second of write churn that would dominate database load while adding no information. Archiving now happens only on a genuine upstream fetch.

### Changed
- **HTTP mode is stateless with plain JSON responses by default** (`--stateful` and `--sse` opt back into sessions and SSE framing). The server declares no `listChanged`/`subscribe` and uses no sampling, elicitation or progress, so sessions bought nothing while costing sticky routing, per-session memory and an event store. Verified: a single bare POST with no `initialize` and no `Mcp-Session-Id` returns all 11 tools and executes tool calls; under `--stateful` the same request is correctly rejected with 400. This removes the last blocker to plain round-robin horizontal scaling now that shared state lives in Postgres.

### Added
- **`ParsedMemo`** (`memo.py`): repositories memoise parsed results in-process, valid while the cache entry's `as_of` is unchanged. Parsing was the dominant per-request cost and produced an identical answer for every user — measured 2.17 ms for the homepage and 1.03 ms for a disclosure page, against 0.04 ms to build models and 0.04 ms to serialise them. End-to-end effect: `get_market_summary` 2.26 ms → ~0 ms per request (~440 → ~500k req/s/core for the parse step), `search_disclosures` 1.12 ms → ~0 ms.
  Safe by construction rather than by a TTL guess: a cached value cannot change before its freeze boundary, so neither can anything derived from it, and a new fetch carries a new `as_of` which misses the memo automatically. Bounded LRU, since cache keys are unbounded. Metadata always comes from the `Served`, never the memo, so `from_cache`/`stale` keep describing the upstream fetch.
- 11 scaling tests, including a guard on the memo-key collision between `get_indices` and `get_market_summary` — they share one cache key but parse it into different shapes, so a key that omitted the projection would silently hand one tool the other's result.

## [0.4.0] - 2026-07-30

### Added
- Phase 4 storage & archive. `PostgresStorage` implements the existing `Storage` protocol, so the market-boundary freeze policy is unchanged — Postgres just makes the cache **shared and durable**, which means N replicas make one upstream fetch per boundary instead of N.
- Opportunistic EOD archive (plan §6a): `eod_bars` and `disclosures` fill from reads a user already triggered, so history deepens with zero extra load on PSE Edge. `Archive` protocol with `NullArchive` (stdio default) and `PostgresArchive`.
- Alembic migrations (`migrations/`, revision `0001_initial`) and a one-shot `migrate` service in compose, gated on `service_completed_successfully`. `check_schema()` fails at startup with an actionable message if migrations were skipped, rather than dying mid-request on an opaque `UndefinedTableError`.
- 20 storage tests against a real ephemeral **Postgres 18** via testcontainers, applying the **real migration** rather than `metadata.create_all` so migration drift is caught in CI. They skip cleanly when Docker is unavailable. 129 tests total.

### Changed
- The runtime image installs `--extra postgres` again, now that code uses the driver — it was correctly absent while nothing imported it. `scripts/check_image.py` takes `--extra` so the expected closure mirrors the Dockerfile; without it the drivers would look like bloat.
- `build_server()` accepts injectable `storage` and `archive`.

### Fixed
- A failed archive write can no longer fail the user's read. The first implementation caught only `SQLAlchemyError`, but an unreachable database raises `OSError`/`ConnectionRefusedError` from the driver socket — so a database outage would have broken every price-history request. Caught broadly now (still letting `CancelledError` through), and a test covers it.
- Postgres stays genuinely optional: the driver modules are imported lazily inside `build_storage()`, so a plain install without the `postgres` extra cannot hit an ImportError. A test asserts SQLAlchemy does not leak into `sys.modules` when the server is imported.

## [0.3.0] - 2026-07-30

### Added
- Phase 3 company & market tools: `get_company_profile`, `get_financial_highlights`, `get_dividends_and_rights`, `get_indices`, `get_market_summary` (11 tools total).
- Five recorded fixtures and 19 new tests (109 total), covering both real captures and the empty/unfiled cases.
- `CompanyInfoRepository` and `MarketRepository`, plus `CompanyInfoSource`/`MarketSource` protocols — Phase 3 added no orchestration to `server.py`, which is what the Phase 2 refactor was for.

### Changed
- `get_indices` and `get_market_summary` share one cached homepage fetch, so asking for both costs PSE Edge a single request.
- `docs/endpoints.md` v4 documents the company-info and market endpoints, replacing the "parse TBD" stubs.

### Fixed
- `search_disclosures(symbol=...)` with no date range returned rows in **no order at all**, so page 1 mixed 2024/2025/2026 filings and "this company's recent disclosures" answered with two-year-old ones — while the tool description promised "newest first". `companyDisclosures/search.ax` ignores `dateSortType` unless `sortType="date"` is also sent, and the client was sending `sortType=""`. Found by manually driving the running server; verified live that `date`+DESC gives newest-first, `date`+ASC oldest-first, and `""` no ordering. `announcements/search.ax` was unaffected — verified byte-identical with and without `sortType`, as it defaults to date DESC. Fixtures re-recorded against the corrected request, and page ordering is now asserted rather than assumed.
- Financial figures are passed through verbatim and never rescaled: Edge's units labels contradict each other between the annual (`Php (in thousands)`) and quarterly (`Php (in Millions)`) sections, with the same figure appearing under both. Each period reports its own `currency_units`, and the tool tells the model to check it before quoting a number.
- Index `change`/`change_percent` are signed. Edge prints them unsigned and encodes direction only in a CSS colour and a ▲/▼ glyph, so a naive parse would report every decline as a gain — PSEi printed `47.29` on a day it fell 47.29.
- `parse_financial_reports` and `parse_market_summary` walk `tree.root.traverse` for true document order. `Node.iter()` sees no nested nodes, and a comma CSS selector returns matches grouped by selector, which filed all four financial statements under the last heading and left the annual section empty.

## [0.2.0] - 2026-07-30

### Added
- Phase 2 disclosures. Tools: `search_disclosures` (market-wide or per company, date-ranged, exact pagination), `search_disclosure_fulltext` (attachment text search with snippets and an honest coverage note), `get_disclosure(edge_no)` (details + attachment/body links).
- Header-driven disclosure table parser: cells map by `<thead>` label, so the differing column layouts of `announcements/search.ax` and `companyDisclosures/search.ax` share one code path and a reordered column is a non-event.
- `immutable=True` in `FreezeService`: objects with a stable natural key (disclosures by `edge_no`) are fetched once and never refetched at a boundary. Reported as `data_policy: "immutable"` with `valid_until: null`.
- `INVALID_ARGUMENT` error code for malformed arguments, rejected before any upstream request.
- Seven recorded disclosure fixtures and 32 new tests (parsers, drift detection, wire dialects, tool routing, cache behaviour).
- Release workflow: merging to `main` builds, gates, smoke-tests, and publishes a multi-arch image (`linux/amd64` + `linux/arm64`) to `ghcr.io/phdwight/pse-edge-mcp` (`:latest` and `:sha-<sha>`). Each arch builds on a native runner and is gated independently, then the digests are combined into one manifest list so a tag is never half-published. A merge that bumps `version` in `pyproject.toml` also cuts a GitHub Release and an immutable `:<version>` tag.
- Branch workflow: `develop` is the integration branch and reaches `main` only by pull request. `main` is protected (PR required, `test` + `image (amd64)` + `image (arm64)` must pass, no force pushes or deletions).
- `tests/test_repositories.py` and `tests/test_validation.py` (23 new tests, 89 total), covering endpoint routing, cache-key distinctness, exact-symbol resolution, URL absolutisation, and every validator.
- `packaging` in the dev group, imported directly by the image check.


### Changed
- `search_disclosures` uses `/announcements/search.ax`, not `/keyword/search.ax` as originally planned: the latter is an attachment full-text index that is partial and stale (measured: nothing from 2026), so it became a separate, clearly-labelled tool. See docs/endpoints.md v3.
- `Meta.valid_until` is now nullable (null for immutable data).
- `docs/endpoints.md` v3 corrects three v2 claims: `search.ax` responses *do* carry `[Total n]` (pagination is exact, no need to iterate until a short page); the disclosure search backend is `announcements`; page size is 10 for `keyword` but 50 for the others.
- **Image gate now enforces necessity rather than a size threshold.** `scripts/check_image.py` asserts the installed distributions equal the runtime closure `uv export` resolves from the lockfile, plus no toolchain/package manager, no bytecode caches, no tests/docs/source tree, and non-root. Size is reported, never gated. The old <200 MB budget passed while the image shipped a package manager, 14 MB of bytecode, an editable-install source tree and (earlier) 44 MB of unused database drivers — and would have failed on a dependency the app genuinely needs.
- Image: `UV_COMPILE_BYTECODE=0` and a `.pyc` sweep in the builder; the project installs `--no-editable` so no source tree or `.pth` indirection ships; `pip` removed from the runtime layer (attack surface only — layers are additive, so base-image bytes stay).
- **Refactored for SOLID.** A domain layer (`repositories.py`) now owns cache key + fetch + parse + model per data domain, so `server.py` is the MCP boundary only: validate, delegate, shape the reply. Endpoint routing moved out of the tools.
  - *SRP:* six copies of the same `try/except PseEdgeMcpError` collapsed into one `reply()` helper; duplicated page/date/edge_no checks extracted to `validation.py`; `has_more` and `CompanyHit` mapping deduplicated; `parse_chart_date` moved off the HTTP client into `parsers.py` where parsing belongs.
  - *DIP + ISP:* new `sources.py` declares narrow per-domain protocols (`CompanySource`, `QuoteSource`, `DisclosureSource`) and `service.FrozenCache`; repositories depend on those, not on `PseEdgeClient`/`FreezeService`, so they are tested with few-line fakes and no HTTP mocking.
  - *OCP:* a new data domain is a new repository, touching neither existing repositories nor the tools.
  - `Served` is now generic (`Served[T]`) with a `.map()` so freshness metadata cannot be dropped while re-typing a payload.
- Tool surface, request wire formats, and response shapes are unchanged; a new `test_tool_surface_is_stable` guards the MCP contract against future refactors.


### Fixed
- `mypy --strict` now passes: generic `dict` annotations parameterised across the package, and dead Pydantic aliases removed from `CompanyHit` (`server.py` already mapped those fields explicitly).
- Image trimmed from 206 MB to 167 MB in CI, which also un-broke the image gate. The runtime image installed `--extra postgres`, but the Postgres backend is Phase 4 and nothing in `src/` imports sqlalchemy, asyncpg, or alembic yet — those four packages were 44 MB of a 103 MB venv. This gate had been failing since the Phase 1 push to `main`.

## [0.1.0] - 2026-07-30

### Added
- Phase 1 scaffold: FastMCP server (stdio + `--http` streamable HTTP).
- Tools: `search_companies`, `get_stock_quote`, `get_price_history`.
- `PseEdgeClient` speaking both verified PSE Edge dialects (JSON-body `.ax`, form-encoded `search.ax`), with token-bucket throttle, retries, and single-flight dedup.
- Market-boundary freeze cache policy (EOD-only; zero upstream traffic while the market is open) with PSE holiday calendar.
- Docker: thin multi-stage image (python:3.14-slim, non-root, secret-free) + Compose v2 stack with Postgres 18.
- Test suite on recorded fixtures (parser, calendar, freeze policy, client transport).
