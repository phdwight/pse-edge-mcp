# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/) · Versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-07-30

### Added
- Phase 2 disclosures. Tools: `search_disclosures` (market-wide or per company, date-ranged, exact pagination), `search_disclosure_fulltext` (attachment text search with snippets and an honest coverage note), `get_disclosure(edge_no)` (details + attachment/body links).
- Header-driven disclosure table parser: cells map by `<thead>` label, so the differing column layouts of `announcements/search.ax` and `companyDisclosures/search.ax` share one code path and a reordered column is a non-event.
- `immutable=True` in `FreezeService`: objects with a stable natural key (disclosures by `edge_no`) are fetched once and never refetched at a boundary. Reported as `data_policy: "immutable"` with `valid_until: null`.
- `INVALID_ARGUMENT` error code for malformed arguments, rejected before any upstream request.
- Seven recorded disclosure fixtures and 32 new tests (parsers, drift detection, wire dialects, tool routing, cache behaviour).
- Release workflow: merging to `main` builds, gates, smoke-tests, and publishes a multi-arch image (`linux/amd64` + `linux/arm64`) to `ghcr.io/phdwight/pse-edge-mcp` (`:latest` and `:sha-<sha>`). Each arch builds on a native runner and is gated independently, then the digests are combined into one manifest list so a tag is never half-published. A merge that bumps `version` in `pyproject.toml` also cuts a GitHub Release and an immutable `:<version>` tag.
- Branch workflow: `develop` is the integration branch and reaches `main` only by pull request. `main` is protected (PR required, `test` + `image (amd64)` + `image (arm64)` must pass, no force pushes or deletions).

### Changed
- `search_disclosures` uses `/announcements/search.ax`, not `/keyword/search.ax` as originally planned: the latter is an attachment full-text index that is partial and stale (measured: nothing from 2026), so it became a separate, clearly-labelled tool. See docs/endpoints.md v3.
- `Meta.valid_until` is now nullable (null for immutable data).
- `docs/endpoints.md` v3 corrects three v2 claims: `search.ax` responses *do* carry `[Total n]` (pagination is exact, no need to iterate until a short page); the disclosure search backend is `announcements`; page size is 10 for `keyword` but 50 for the others.

### Fixed
- `mypy --strict` now passes: generic `dict` annotations parameterised across the package, and dead Pydantic aliases removed from `CompanyHit` (`server.py` already mapped those fields explicitly).
- Image size back inside the <200 MB budget of invariant #5 (206 MB → 167 MB in CI). The runtime image installed `--extra postgres`, but the Postgres backend is Phase 4 and nothing in `src/` imports sqlalchemy, asyncpg, or alembic yet — those four packages were 44 MB of a 103 MB venv. This gate had been failing since the Phase 1 push to `main`.

## [0.1.0] - 2026-07-30

### Added
- Phase 1 scaffold: FastMCP server (stdio + `--http` streamable HTTP).
- Tools: `search_companies`, `get_stock_quote`, `get_price_history`.
- `PseEdgeClient` speaking both verified PSE Edge dialects (JSON-body `.ax`, form-encoded `search.ax`), with token-bucket throttle, retries, and single-flight dedup.
- Market-boundary freeze cache policy (EOD-only; zero upstream traffic while the market is open) with PSE holiday calendar.
- Docker: thin multi-stage image (python:3.14-slim, non-root, secret-free) + Compose v2 stack with Postgres 18.
- Test suite on recorded fixtures (parser, calendar, freeze policy, client transport).
