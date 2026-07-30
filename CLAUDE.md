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
5. **Docker images stay thin and secret-free.** Multi-stage, non-root, no build tools
   in runtime, no credentials in any layer/ARG/ENV. CI enforces <200 MB + secret scan.

## Conventions

- Python 3.14, `uv` for everything (`uv sync --all-extras`, `uv run pytest`, `uv run ruff check .`).
- ruff line-length 100; mypy strict; pytest-asyncio in auto mode.
- Compose v2 (`compose.yaml`, no `version:` key). `docker compose up` = app + Postgres 18.
- Every tool result returns `{"data": ..., "meta": {as_of, valid_until, from_cache, stale, data_policy}}`.
- Timestamps ISO-8601 in Asia/Manila. Accounting negatives `(1,234)` → `-1234`.
- Layering: `server.py` (MCP tools) → `service.py` (freeze policy) → `client.py`
  (pure HTTP, MCP-agnostic) + `parsers.py` (HTML → dicts). Keep it that way.

## Roadmap (details in docs/plan.md §7)

- **Phase 2 (next):** disclosures — `search_disclosures` (`/keyword/search.ax` market-wide
  + `/companyDisclosures/search.ax` per-company, HTML-fragment parsing, 50 rows/page,
  paginate until short page), `get_disclosure(edge_no)` via `openDiscViewer.do`,
  attachment links via `downloadFile.do?file_id=` / `downloadHtml.do?file_id=`.
- **Phase 3:** financial reports, dividends & rights, indices, market summary (all HTML parses).
- **Phase 4:** Postgres storage backend (same `Storage` protocol) + Alembic + opportunistic archive.
- **Phase 5:** OAuth 2.1 (Authlib) + passkey signup (py_webauthn), tiered quotas, admin CLI.
- **Phase 6:** production deploy (compose.prod.yaml overlay, TLS, backups).

## Holiday table

`market_calendar.PSE_HOLIDAYS` is a yearly-maintained seed — verify against the PSE
holiday circular each December and when touching calendar logic.
