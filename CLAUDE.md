# CLAUDE.md

MCP server exposing Philippine Stock Exchange (PSE Edge, https://edge.pse.com.ph) data.
Unofficial — Edge has no public API; we speak to the portal's own internal endpoints.

**Read these first — they are the project's memory:**

- `docs/plan.md` — every design decision: scope, architecture, caching policy, auth design,
  delivery history, risks. Treat decided items as settled unless the user says otherwise.
- `docs/endpoints.md` — the verified endpoint map: request dialects, param names, response
  shapes, pagination. Trust this over guesses.

This file holds only what applies to every task. Task-specific rules (caching mechanics,
auth invariants, release procedure, deploy/debug, subsystem gotchas) live in the agent
memory index — load the matching topic file before working in that area.

**Memory write rule:** when recording a new durable instruction ("remember that…", a
correction, a learned fact), create or edit the one single-purpose topic file it belongs
to under the memory directory and update its line in the index. Never append to this file
without asking — it is always loaded, and unchecked additions re-monolithize it.

## Non-negotiable invariants

1. **Never refetch a cached price while the market is open** (09:30–15:00 Asia/Manila,
   trading days). Every upstream read goes through `FreezeService.get(..., policy=...)` —
   never call `PseEdgeClient` directly from a tool. The default policy is the strictest,
   so an unlabelled read can only over-protect PSE Edge.
2. **Tests never touch PSE Edge.** All HTTP is mocked with respx against fixtures in
   `tests/fixtures/` (recorded from real captures). A new endpoint needs a new fixture.
3. **Loud on drift.** If Edge's response shape changes, raise `EndpointChangedError` —
   never silently return partial data.
4. **No credentials anywhere:** not in commits, image layers, ARGs, or ENV.

## Architecture

```
server.py        MCP boundary only: validate args, delegate, shape the reply.
  ↓
repositories.py  One repository per data domain: cache key + freeze read + parse +
                 model. Endpoint routing lives here.
  ↓
service.py       Freeze policy. Repositories depend on the FrozenCache protocol and
sources.py       the narrow per-domain source protocols — never the concrete client.
  ↓
client.py        Pure HTTP.  parsers.py  HTML/JSON → dicts.
```

No domain logic, cache keys, or parsing in `server.py`. Read tools return
`{"data": ..., "meta": ...}`; action tools return no `meta`.

## Conventions

- Python 3.14; `uv` for everything (`uv sync --all-extras`, `uv run pytest`,
  `uv run ruff check .`); ruff line-length 100; mypy strict; pytest-asyncio auto mode.
- Git: all work lands on `develop`; never commit to `main` (protected — it is reached
  only by PR, and merging publishes a release, so opening PRs and merging are the user's
  explicit calls). Never `git add -A` — stage files by name.
- Respond tersely: lead with the outcome; add only findings that change what the user
  does next. Still report failures and skipped steps plainly.
