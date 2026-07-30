# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/) · Versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-07-30

### Added
- Phase 1 scaffold: FastMCP server (stdio + `--http` streamable HTTP).
- Tools: `search_companies`, `get_stock_quote`, `get_price_history`.
- `PseEdgeClient` speaking both verified PSE Edge dialects (JSON-body `.ax`, form-encoded `search.ax`), with token-bucket throttle, retries, and single-flight dedup.
- Market-boundary freeze cache policy (EOD-only; zero upstream traffic while the market is open) with PSE holiday calendar.
- Docker: thin multi-stage image (python:3.14-slim, non-root, secret-free) + Compose v2 stack with Postgres 18.
- Test suite on recorded fixtures (parser, calendar, freeze policy, client transport).
