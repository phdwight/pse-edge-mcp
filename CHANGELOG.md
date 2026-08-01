# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/) · Versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **Docs re-synced against 0.8.x.** The walkthrough had drifted in a way worth naming: it stated *"There is no `client_credentials` grant — both supported grants are browser-bound"*, which stopped being true one release earlier. A doc that is merely incomplete costs a reader time; one that is confidently wrong sends them somewhere else. It now documents the grant, the machine-only gate and why it cannot depend on request data, the service-user design, the token-endpoint rate limiter, the new `oauth_clients` columns, four new token-endpoint entries in the symptom table, and a "reading the log" table naming each critical-path line. Module line counts, model and test counts refreshed against the code.
- `CLAUDE.md` gains Phase 5 stage 3 and the logging rules; `docs/plan.md` §6 records the `client_credentials` decision and the three that support it (service user, no refresh token, rate-limited token endpoint), since it is the file that explains *why*.

## [0.8.1] - 2026-08-01

### Added
- **INFO logging on the critical paths**, so an operator can answer the questions that matter without a debugger. Every **upstream fetch to PSE Edge** is logged with its cache key and duration — that is the line this whole server exists to keep rare, and one appearing during market hours means the freeze invariant is broken. Also logged: refusing an uncached read while the market is open, bearer-token rejections and quota refusals (refusals only — a successful call is already an access-log line, and repeating it would double the hot path to say nothing new), the OAuth lifecycle (dynamic registration, machine-client provisioning and revocation, consent, code exchange, token issuance), and email delivery. Two get **WARNING** because they mean something is wrong rather than merely happening: refresh-token reuse (a rotated token presented twice has leaked) and a non-machine client being denied `client_credentials`.
- **A startup line stating the resolved configuration** — version, public URL, auth on/off, storage and archive backend, transport mode, email sender, market hours. "What config is this actually running?" is the first question in any incident, and answering it from env vars and compose files is guesswork. No secrets: only whether each is present.

### Fixed
- **Plain-format log lines had no timestamp at all.** The JSON formatter always emitted one, but `PSE_LOG_JSON` is off by default, so the format a developer reads in a terminal — and the one an unconfigured deployment writes to its container log — produced lines that could not be correlated with an incident, a user's report, or even the line above them after a restart. Both formats now emit the same ISO-8601 stamp with an explicit offset. The plain formatter also applies the same secret redaction as the JSON one, which it previously did not — leaving the safer-looking format as the leakier one.

## [0.8.0] - 2026-08-01

### Added
- **`client_credentials` grant, so headless agents can authenticate without a browser.** A LangGraph app or the Anthropic Messages API MCP connector can now exchange a client id and secret for a bearer token at `POST /oauth/token`. Client authentication accepts HTTP Basic and form-body credentials, secrets are compared in constant time against a SHA-256 stored at rest, `scope` is validated (`mcp` only — an unknown scope is refused rather than silently pruned), and the RFC 8707 `resource` parameter is checked against this server's canonical `/mcp` URL when supplied. Access tokens are minted into the same table with the same `kind='access'` and the same hashing as the authorization-code flow, so `/mcp` bearer validation needed **no change at all**. Expiry 1 hour, and no refresh token: the client already holds a long-lived secret, so a refresh token would be a second credential of equal power without the rotation benefit that justifies one.
- **The grant is refused for every client that registered itself.** `/oauth/register` is open by design, so authorization to use `client_credentials` cannot be derived from anything a registrant supplies — not a `grant_types` array, not a requested auth method, not the presence of a secret. It is gated on an `oauth_clients.client_type` column that only the admin CLI writes; a DCR client that declares the grant and sends a secret still gets `unauthorized_client`. This is the single most important behaviour in the change and has its own test.
- **`pse-edge-admin create-machine-client / revoke-machine-client / list-machine-clients`.** Provisioning generates a 48-byte secret, prints it once, and stores only its hash. Each machine client is backed by a service account under the reserved, non-routable `machine.invalid` domain — which is what lets the bearer path stay identical (token lookup joins `auth_tokens` to `users`) and gives machine clients quotas, usage accounting and disablement for free. Revoking clears the secret, revokes every token the client minted, and disables the service account in one step.
- **`validate_symbol(symbol)`** — a cheap yes/no ticker check for agents, returning `valid`, the normalised uppercase symbol, and the company name and id when it exists. Unknown symbols answer `valid: false` with nulls rather than raising `SYMBOL_NOT_FOUND`: an agent checking a symbol is asking a question, and "no" is a good answer to it. Matching reuses `CompanyRepository`'s exact case-insensitive rule via a new `try_resolve`, so there is still only one implementation of "is this the right company" — two would eventually disagree, and the drifted one would answer about the wrong company rather than failing visibly.
- Rate limiting on `POST /oauth/token`, counted per `client_id` **and** per client IP (20/minute each), answering `429` with `Retry-After`. Both keys are always counted, so an attacker cannot keep one counter cold by tripping the other.

### Fixed
- **Client secrets were not redacted from logs.** The redaction pattern used `\bsecret\b`, and `_` is a word character — so there is no word boundary before `secret` in `client_secret`, and the pattern never matched it. Anything that echoed a token-endpoint form would have logged the secret verbatim. The pattern now allows a prefix (catching `client_secret`, `api-key`, `x_auth_token`), and a `Basic <base64>` credential is redacted whole, since the secret is inside the blob.

### Changed
- `/.well-known/oauth-authorization-server` advertises `client_credentials` in `grant_types_supported` and `client_secret_basic` / `client_secret_post` in `token_endpoint_auth_methods_supported`. Advertising is not authorization — the machine-only gate still applies to every caller.

### Added
- **`docs/walkthrough.md` — a developer/architect walkthrough**, plus a rendered PDF. Written against the code rather than from memory: the request lifecycle from client to PSE Edge, the freeze decision table, the four-layer architecture and why repositories depend on Protocols, all 11 tools with their arguments and owning repositories, the response envelope and error codes, the OAuth/passkey flow, extension recipes, and a symptom-to-cause debugging table carrying the failures production found (421 Host header, empty logs meaning a start-time failure, ZeptoMail's exact-domain rule). `scripts/render_doc_pdf.py` regenerates the PDF with any Chromium — no pandoc or LaTeX toolchain, and diagrams are ASCII so they survive every renderer and stay readable on GitHub.

## [0.7.3] - 2026-07-31

### Fixed
- **`POST /mcp` returned `421 Invalid Host header` behind any reverse proxy.** The SDK's DNS-rebinding guard allows `localhost` and `127.0.0.1` only unless told otherwise, and production never told it otherwise — so behind Caddy or a Cloudflare Tunnel, where the `Host` header carries the public hostname, every real request was refused. The failure was unusually hard to read: registration, authorize, consent and token exchange all succeeded, the client stored a valid token, and only the first tool call failed. The allowlist is now derived from `PSE_PUBLIC_URL` (both the bare host and `:443`, since proxies differ on whether `Host` carries a port), so there is nothing new to configure. The guard is kept rather than disabled — auth stops a stranger calling the API, this stops a browser on the victim's own network reaching a server that trusts its network position.
  - **248 tests did not catch it because the journey test passed `transport_security` explicitly**, exercising a configuration production never built. The new tests go through `create_app()` and assert a proxied `Host` does not 421.
- **`serverInfo.version` was an empty string.** `MCPServer(...)` was constructed without a `version`, so every `initialize` response told the connecting client the server had no version. It now reports the installed distribution version, read from `importlib.metadata` so it cannot drift from `pyproject.toml`.
- **Signing in without an OAuth flow no longer lands on a bearer-token error.** A person who went to `/login` directly — rather than being sent there by an MCP client — was redirected to the bare public URL on success. `/` was not a route, so it fell through to the MCP endpoint and answered a freshly authenticated user with `{"error": "UNAUTHORIZED", "message": "Missing bearer token."}`, asking them for something they had no way to obtain. They now land on `/account`.
- **`/` is a page.** It shows the MCP endpoint URL to paste into a client, notes that the client will bring you back to authorize so signing up first is unnecessary, and links signup, sign-in and privacy. An authenticated visitor is redirected to `/account`. Returning an API error at the front door of a public service was wrong regardless of how anyone arrived there.
- `/favicon.ico` answers 204 instead of falling through to a 401. Every browser asks for it, and the refusals were noise in logs being read to debug something real.

## [0.7.2] - 2026-07-31

### Fixed
- `PSE_EMAIL_FROM` moved into `.env.example`'s Email section. It had been documented only under "NAS stage 2", so a reader scanning for it where it belongs did not find it — and it is not NAS-specific: both compose files derive a default from `PSE_DOMAIN` and both get it wrong when the hostname is a subdomain. It now carries a curl that tests a sender against ZeptoMail directly, since the failure gives a bare 500 with an empty body and cannot be diagnosed from the app's logs.
- Corrected a stale claim in `compose.nas.yaml`'s header, which still said the tunnel overlay unpublishes the LAN port — untrue since `!reset` was removed in 0.7.1, and contradicted by the file's own body twenty lines down. Documented the two traps hit while deploying: a taken `PSE_LAN_PORT` leaves the app container created with **no logs at all** (the process never ran, so an empty log *is* the symptom), and `PSE_EMAIL_FROM` must be on a domain ZeptoMail has verified.
- **A failing mail provider no longer answers signup with a 500.** Seen in production: ZeptoMail returned 500 with an empty body, the exception escaped the handler, and a user typing their address got a bare "Internal Server Error" — which invites them to conclude the address was at fault and try a different one, which cannot help. Signup now answers **503** with "this is our problem, try again in a few minutes", and the detail goes to the log where an operator can act on it. The signup token is already stored by then, so retrying genuinely works.
- **The send failure now names the sender address**, and says `<empty body>` rather than nothing when the provider returns one. An unverified sender is by far the most common cause and ZeptoMail often reports it as a bare 500, so the old message identified neither the problem nor the value that caused it. Note ZeptoMail verifies **exact** domains: a verified `example.com` does not cover `sub.example.com`.
- **`compose.prod.yaml` now says that `Caddyfile` must sit beside it.** It is bind-mounted, and Docker cannot mount a file that does not exist, so importing the compose file on its own leaves `caddy` *created but never started* with an opaque OCI "not a directory" error while every other service comes up — which in a NAS UI reads as the project being broken for no visible reason. `compose.nas.yaml` mounts no repository files, so a single-file import of that one is complete.
- **Documented that `migrate` exiting is success, not failure.** It runs `alembic upgrade head` once and exits 0, because the schema must not be applied by the server on boot (replicas would race to mutate it). NAS Docker UIs list any stopped container as "Not in use" and colour the whole project red on that basis, so a healthy stack looks broken; the deploy guide now gives the expected per-container states and says what a genuine failure looks like instead.

### Added
- **README: the end-to-end flow for connecting to a hosted server.** A deployment with auth on is an ordinary OAuth 2.1 protected resource, so a modern MCP client needs only the URL — but nothing said so, and the auth section still claimed the self-service flow had not shipped. It now walks the sequence a client actually performs (401 → resource metadata → AS metadata → dynamic registration → PKCE authorize → signup/passkey → consent → code exchange → bearer), names the only two steps a human performs, and gives the operator-issued-token path for clients that do not speak OAuth yet — which is also the only route on a LAN deployment, since passkeys need a secure context.

## [0.7.1] - 2026-07-31

### Fixed
- **No compose file uses `!reset` any more.** It is a Docker-Compose-only YAML tag, and a conforming parser rejects an unknown tag outright — so NAS Docker UIs, PaaS importers and editor linters flagged `compose.prod.yaml` and `compose.tunnel.yaml` as errors while `docker compose` itself was perfectly happy. The file was only broken where it gets deployed, which is the worst place to find out.
  - `compose.prod.yaml` is now **standalone** rather than an overlay on the development file. It no longer inherits a `build:` directive and a published port only to undo them, and it can be imported by a UI that takes one file. Run it with `-f compose.prod.yaml` alone.
  - The tunnel overlay can no longer close the stage 1 LAN port for you: **Compose merges `ports` additively**, so a second file can add a mapping but never remove one, and `!reset` was the only mechanism. Set `PSE_LAN_BIND=127.0.0.1` in `.env` instead, which moves the port to the host's own loopback — verified from a second machine, LAN access refused while loopback still answers.
- A test now fails on any Compose-only YAML tag in any `compose*.yaml`, since `docker compose config` accepts them and nothing else in CI would have noticed.

## [0.7.0] - 2026-07-31

### Changed
- **Published ports moved out of the contested range** for hosts that already run other things. Caddy publishes 8280/8243 instead of 80/443, and the NAS LAN port defaults to 8200 rather than 8000 — one of the most contested numbers on a NAS. Only the published side moves in both cases: Caddy still listens on 80/443 inside its container and the app on 8000 inside its own, so the Caddyfile and the tunnel's `app:8000` route are unchanged. Overridable via `PSE_HTTP_PORT`, `PSE_HTTPS_PORT` and `PSE_LAN_PORT`. **The Caddy path now requires the router to forward 80 → 8280 and 443 → 8243**, because ACME validates on 80 (HTTP-01) or 443 (TLS-ALPN-01) and no setting changes those numbers; where that forwarding is impossible, the tunnel path needs no inbound ports at all.
- **Production pulls the published image instead of building from source.** `compose.prod.yaml` inherited `build: .` from the dev base, so a deployment rebuilt from whatever the host's source tree and Docker cache happened to contain rather than running the artifact CI actually gated — size-checked, secret-scanned, smoke-tested, multi-arch. `app`, `migrate` and `purge` now all pull the same `PSE_IMAGE_TAG`; `purge` had additionally been pinned to a hardcoded `:latest`, so it could run a different build than the app it purges for.
- **The NAS deployment is now two stages**, because a public hostname is not something you have on day one. `compose.nas.yaml` alone is a complete LAN deployment on `http://<nas-ip>:8000` that needs no Cloudflare account, domain or token — those variables live in the new `compose.tunnel.yaml`, and Compose interpolates required variables before it filters services, so keeping them in a separate file is what makes stage 1 possible at all rather than merely tidy. Adding the overlay starts `cloudflared` *and* unpublishes the LAN port, so going public closes the local door in the same action instead of leaving it to be remembered.

## [0.6.0] - 2026-07-31

### Added
- **Phase 6 — production deployment.** `compose.prod.yaml` + `Caddyfile` + `docs/deploy.md`: TLS with automatic ACME renewal, auth on by default, restart policies, resource limits, log rotation, daily `pg_dump` with rotation, and a daily usage-retention purge. Neither `app` nor `db` publishes a port — everything arrives through the proxy, so a stray firewall rule cannot expose the app unencrypted or the database at all.
- **`pse_edge_mcp.asgi:app`** — an importable ASGI application, so `uvicorn … --workers N` and gunicorn can run this. uvicorn's multi-worker supervisor forks and re-imports, so it needs an import string; an object built inside `main()` cannot be shared with the children. It also means the CLI and production compose the *same* stack instead of two that drift. Resolved lazily (PEP 562) so importing the module opens no connections.
- **Health endpoints**: `/health` (liveness, dependency-free) and `/health/ready` (readiness, checks the database, 503 when unreachable). Both are unauthenticated — a probe cannot hold a token — and Caddy hides them from the internet. Keeping them separate matters: a liveness probe that touches Postgres restarts every replica during a blip, turning a recoverable outage into a restart storm.
- **Structured JSON logging** (`PSE_LOG_JSON=1`), stdlib-only, with secret redaction as a backstop and uvicorn's own handlers routed through the same formatter.
- `--workers` on the CLI, and 20 new tests (240 total) covering the probes, the formatter, and the factory.
- **`compose.nas.yaml`** — a standalone deployment for a NAS behind a **Cloudflare Tunnel**: no inbound ports at all, no port forwarding, nothing for CGNAT to break, and no contest with the NAS web UI over 80/443. Cloudflare terminates TLS at its edge, so no Caddy or ACME is involved. Standalone rather than an overlay because NAS Docker UIs import a single file far more happily, and it pulls the published image rather than building, since a NAS is a poor build host. Both it and `cloudflared` are multi-arch, so Intel and ARM models are covered.
- **Privacy compliance (plan §6a), the obligations that arrive with a real user's email address.** A public `/privacy` page stating what is collected, how long it is kept and who to contact about a breach; an `/account` subject-access view; and self-service `POST /account/delete` that erases immediately — no request, no waiting period, no email exchange.
- **Per-user usage log with 90-day retention**, aggregated per user-hour rather than per request. That holds markedly less about a person (itself the §6a minimal-collection requirement), keeps writes off the request path, and makes retention an indexed range delete. Counts buffer in memory and flush on an interval; a clean shutdown flushes what is pending, and a failing sink is logged rather than raised into the request.
- `pse-edge-admin delete-user` (refuses without `--yes`) and `purge-usage` for a daily cron. `delete-user` reuses the user's own erasure path, so the operator route cannot drift from the promise made on the privacy page.
- Disposable-email domains refused at signup (plan §6's abuse brake), and CSRF tokens on `/consent` and `/account/delete` — SameSite=Lax already blocks the cross-site POST, so this is defence in depth.
- 20 new tests (220 total). `test_erasure_leaves_nothing_behind` walks `metadata.tables` rather than a hand-written list, so a table added later that references a user fails the test instead of silently retaining personal data after a deletion.

### Changed
- `configure_logging` replaces only handlers it installed. Clearing every root handler would have been idempotent by stomping on whatever else was listening — pytest's capture, or a host application that embedded this server.
- Erasure is a hard delete in one transaction, never a `disabled_at` flag: a soft delete leaves the email address on file, which is the opposite of erasure and would make the privacy page's promise false. Public market data (`eod_bars`, `disclosures`) survives, because it was never about the user.

## [0.5.0] - 2026-07-30

### Added
- **Phase 5 stage 2 — OAuth 2.1 and passkeys.** Self-service signup with no passwords anywhere: email verification link → WebAuthn passkey enrollment → dynamic client registration (RFC 7591) → authorize with mandatory S256 PKCE → consent → token. Discovery via RFC 9728 protected-resource metadata and RFC 8414 authorization-server metadata, and 401s now carry `WWW-Authenticate: Bearer resource_metadata="…"` so a client can find the authorization server instead of hitting a dead end. Refresh tokens rotate; replaying a rotated one is treated as theft and revokes the whole family (RFC 9700 §4.14). New modules: `oauth.py`, `passkeys.py`, `auth_app.py`, `email.py`; migration `0003_oauth`.
- ZeptoMail transactional email (console sender when no key is configured). Unlike archive writes, a send failure is raised rather than swallowed — a user who never receives their link experiences "signup is broken".
- 46 new tests (201 total), including a full journey through the real ASGI stack against real Postgres: signup → passkey enrollment → DCR → authorize → passkey login → consent → PKCE exchange → authenticated `/mcp` call → refresh. `soft-webauthn` provides real WebAuthn signatures, so the ceremonies are genuinely verified without a browser. Attack cases are covered explicitly: open-redirector refusal, prefix-matching redirect URIs, PKCE downgrade to `plain`, code replay, consent replay, refresh reuse, tampered assertions, challenge replay, refresh-as-bearer, and account enumeration via the signup response.
- **Phase 5 stage 1 — bearer auth and quotas, opt-in** (`PSE_AUTH_REQUIRED=1`, needs `DATABASE_URL`; stdio never authenticates). Opaque `pse_`-prefixed tokens stored as SHA-256 only; validation is one indexed lookup fronted by a cache whose **TTL is the revocation-latency budget and nothing else** (default 60 s, `PSE_TOKEN_CACHE_TTL`); refusals are never cached, so a just-issued token works immediately. Per-user quotas (60/min, 2,000/day defaults, per-user overrides) counted in fixed in-process windows; over-limit answers 429 with `Retry-After` and a `RATE_LIMITED` body. New `pse-edge-admin` CLI (create-user, issue-token, revoke-token, disable-user, set-quota, list-users), migration `0002_auth`, and 34 new tests including a live end-to-end pass: 401 without a token, 200 with one, 429 past quota, and revocation landing within the documented budget.
- Connection-pool sizing is configurable (`PSE_DB_POOL_SIZE`, `PSE_DB_MAX_OVERFLOW`) — auth turns every request into a potential DB lookup, so the pool stops being a fixed default.

### Changed
- **Authlib dropped from the design** (plan §6, at the revisit point the plan itself scheduled). Verified empirically: Authlib 1.7 ships OAuth *server* integrations for Flask and Django only — `starlette_client` is the client side — so adopting it meant bending a Flask-shaped server onto Starlette for a surface small enough to enumerate. `oauth.py` implements it directly, with each rule pinned by a test.
- `auth_store` now requires `kind='access'`, so refresh tokens sharing the table can never be presented as bearer tokens.
- The image and both image gates install the new `auth` extra, since code imports `webauthn`.
- `docs/plan.md` §6 rewritten: the opaque-over-JWT rationale no longer rests on "quotas need a DB hit per request anyway" (quotas now count in-process — a per-request counter UPDATE is the same hot-row defect class as the archive-on-cache-hit bug). Opaque tokens stay, for instant revocation bounded by the cache TTL. Transactional email decided: **ZeptoMail** (key via env at runtime only).
- `build_storage()` now also returns the engine, so auth shares the storage pool instead of opening a second one.

### Fixed
- `get_price_history` no longer passes through PSE Edge's own duplicate rows: identical repeated trade dates are collapsed (observed live: Jul 21 2026 twice, so a range reported 22 bars for 21 trading days and day-counting consumers double-counted). A date repeating with *different* values raises `ENDPOINT_CHANGED` — that is drift we don't understand, and invariant #4 says be loud rather than guess.
## [0.4.0] - 2026-07-30

### Added
- Phase 4 storage & archive. `PostgresStorage` implements the existing `Storage` protocol, so the market-boundary freeze policy is unchanged — Postgres just makes the cache **shared and durable**, which means N replicas make one upstream fetch per boundary instead of N.
- Opportunistic EOD archive (plan §6a): `eod_bars` and `disclosures` fill from reads a user already triggered, so history deepens with zero extra load on PSE Edge. `Archive` protocol with `NullArchive` (stdio default) and `PostgresArchive`.
- Alembic migrations (`migrations/`, revision `0001_initial`) and a one-shot `migrate` service in compose, gated on `service_completed_successfully`. `check_schema()` fails at startup with an actionable message if migrations were skipped, rather than dying mid-request on an opaque `UndefinedTableError`.
- **`ParsedMemo`** (`memo.py`): repositories memoise parsed results in-process, valid while the cache entry's `as_of` is unchanged. Parsing was the dominant per-request cost and produced an identical answer for every user — measured 2.17 ms for the homepage and 1.03 ms for a disclosure page, against 0.04 ms to build models and 0.04 ms to serialise them. End-to-end effect: `get_market_summary` 2.26 ms → ~0 ms per request (~440 → ~500k req/s/core for the parse step), `search_disclosures` 1.12 ms → ~0 ms.
  Safe by construction rather than by a TTL guess: a cached value cannot change before its freeze boundary, so neither can anything derived from it, and a new fetch carries a new `as_of` which misses the memo automatically. Bounded LRU, since cache keys are unbounded. Metadata always comes from the `Served`, never the memo, so `from_cache`/`stale` keep describing the upstream fetch.
- 11 scaling tests, including a guard on the memo-key collision between `get_indices` and `get_market_summary` — they share one cache key but parse it into different shapes, so a key that omitted the projection would silently hand one tool the other's result.
- 20 storage tests against a real ephemeral **Postgres 18** via testcontainers, applying the **real migration** rather than `metadata.create_all` so migration drift is caught in CI. They skip cleanly when Docker is unavailable. 129 tests total.

### Changed
- **HTTP mode is stateless with plain JSON responses by default** (`--stateful` and `--sse` opt back into sessions and SSE framing). The server declares no `listChanged`/`subscribe` and uses no sampling, elicitation or progress, so sessions bought nothing while costing sticky routing, per-session memory and an event store. Verified: a single bare POST with no `initialize` and no `Mcp-Session-Id` returns all 11 tools and executes tool calls; under `--stateful` the same request is correctly rejected with 400. This removes the last blocker to plain round-robin horizontal scaling now that shared state lives in Postgres.
- The runtime image installs `--extra postgres` again, now that code uses the driver — it was correctly absent while nothing imported it. `scripts/check_image.py` takes `--extra` so the expected closure mirrors the Dockerfile; without it the drivers would look like bloat.
- `build_server()` accepts injectable `storage` and `archive`.

### Fixed
- **Archiving ran on cache hits.** `search_disclosures` and `get_price_history` recorded to the archive unconditionally, so a request served entirely from cache still wrote up to 50 `ON CONFLICT DO NOTHING` rows. At 1,000 req/s that is ~50k no-op upserts per second of write churn that would dominate database load while adding no information. Archiving now happens only on a genuine upstream fetch.
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
