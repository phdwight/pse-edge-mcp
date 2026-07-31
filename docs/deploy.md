# Deploying pse-edge-mcp

A production deployment is one command once DNS and `.env` are in place. This page covers
what to set, why the pieces are arranged as they are, and what to check afterwards.

```bash
cp .env.example .env      # then fill in the required values below
docker compose -f compose.yaml -f compose.prod.yaml up -d
```

## What the overlay adds

| Service | Role |
|---|---|
| `caddy` | TLS termination; obtains and renews certificates over ACME automatically |
| `migrate` | one-shot `alembic upgrade head`; `app` waits for it to succeed |
| `app` | the server, 2 workers, auth on, JSON logs, no published ports |
| `db` | Postgres 18, no published ports |
| `backup` | daily `pg_dump` at 02:00 Asia/Manila, rotated |
| `purge` | daily usage-retention purge |

Neither `app` nor `db` publishes a port: everything arrives through Caddy. A stray firewall
rule therefore cannot expose the app unencrypted or the database at all.

## Required configuration

```bash
PSE_DOMAIN=mcp.example.com            # Caddy gets a certificate for this
PSE_ACME_EMAIL=ops@example.com        # CA contact address
POSTGRES_PASSWORD=<long random value>
ZEPTOMAIL_API_KEY=<key>               # verification emails; runtime only, never committed
```

**`PSE_PUBLIC_URL` must be the externally reachable https URL.** The overlay derives it from
`PSE_DOMAIN`, so setting that correctly is enough. It drives three things at once: the
WebAuthn `rp_id`, the links in verification emails, and the OAuth issuer in the discovery
documents. Getting it wrong breaks passkeys in a way that looks like a browser bug, because
WebAuthn binds every credential to the origin it was created under — credentials enrolled
against the wrong origin cannot be recovered, only re-enrolled.

## Health checks

- `GET /health` — **liveness**. Cheap, no dependencies. This is what the container
  healthcheck uses.
- `GET /health/ready` — **readiness**. Verifies the database; answers `503` when it is
  unreachable.

The split matters. A liveness probe that touches the database restarts every replica during
a database blip, turning a recoverable outage into an outage plus a restart storm. Readiness
failing should remove a replica from rotation; liveness failing should kill it. Caddy returns
404 for both paths, so they are available to the orchestrator but not to the internet.

## Workers and what is shared

`--workers 2` in the overlay. Each worker is a separate process that re-imports the app, so
**every in-memory structure is per worker**: quota windows, the parse memo, the token
validation cache, and the usage buffer.

The consequence to understand before raising it: a user's effective quota ceiling becomes up
to *N ×* the nominal limit. That is the same trade already accepted across replicas — quotas
exist to stop abuse, not to bill exactly — but 16 workers means a 16× looser limit, and the
parse memo is warmed 16 times over. Scale workers for CPU, and scale limits with them.

Anything that must be shared already lives in Postgres: the freeze cache, the archive,
accounts, tokens and OAuth state. That is what makes replicas and workers interchangeable.

## Backups

`pg_dump --format=custom` daily at 18:00 UTC (02:00 Asia/Manila) into `./backups`, retained
for `BACKUP_RETENTION_DAYS` (default 14). The timing is deliberate: after the market close
and away from the boundary refetch, so a dump never competes with the day's one upstream
fetch. Rotation runs **only after a successful dump**, so a failing backup never prunes the
good ones it was supposed to replace.

Restore with:

```bash
docker compose exec -T db pg_restore -U pse -d pse_edge --clean /backups/<file>.dump
```

The named volume plus these dumps cover the two unrecoverable assets: user accounts and the
accumulated EOD archive. Everything else can be re-fetched from PSE Edge.

**Test a restore before you need one.** An untested backup is a hypothesis.

## First-run checklist

```bash
docker compose -f compose.yaml -f compose.prod.yaml ps          # all services up, app healthy
curl -fsS https://$PSE_DOMAIN/health                            # 200 via TLS
curl -fsS https://$PSE_DOMAIN/.well-known/oauth-protected-resource
curl -fsS -o /dev/null -w '%{http_code}\n' -X POST https://$PSE_DOMAIN/mcp   # expect 401
```

That last 401 is the point: it confirms auth is on. It should carry a `WWW-Authenticate`
header naming the metadata URL, which is how MCP clients discover where to authenticate.

Then visit `https://$PSE_DOMAIN/signup` and complete a real signup — email, passkey — to
confirm `PSE_PUBLIC_URL` and email delivery are both right. Doing this once, deliberately, is
much cheaper than discovering an origin mismatch from a user's bug report.

## Running under another ASGI server

The app is importable, so it is not tied to the bundled CLI:

```bash
uvicorn pse_edge_mcp.asgi:app --workers 4
gunicorn -k uvicorn.workers.UvicornWorker pse_edge_mcp.asgi:app
```

Configuration comes from the environment in both cases — a worker subprocess re-imports the
module rather than re-parsing a command line.

## Operations

```bash
pse-edge-admin list-users
pse-edge-admin issue-token you@example.com --note laptop   # shown once
pse-edge-admin revoke-token <token>
pse-edge-admin disable-user someone@example.com            # keeps the account, kills access
pse-edge-admin delete-user someone@example.com --yes       # erases everything (§6a)
pse-edge-admin purge-usage --retention-days 90
```

Revocation takes effect within `PSE_TOKEN_CACHE_TTL` (60 s default). That TTL is the
revocation-latency budget and nothing else — lowering it costs database reads, raising it
lengthens the window in which a revoked token still works.

---

# NAS + Cloudflare Tunnel

The topology most home deployments actually want: a NAS with no ports open to the internet,
reached through a Cloudflare subdomain. `cloudflared` dials **out** to Cloudflare, so there
is no port forwarding, nothing for CGNAT to break, no dynamic-DNS to maintain, and no
contest with the NAS's own web UI over ports 80 and 443.

Use `compose.nas.yaml`, which is standalone rather than an overlay — NAS Docker UIs import
a single file much more happily — and **pulls** the published image instead of building,
since a NAS is a poor build host.

```bash
docker compose -f compose.nas.yaml up -d
```

## Setting it up

1. **Cloudflare Zero Trust → Networks → Tunnels → Create a tunnel** (type: Cloudflared).
   Copy the token it shows you.
2. Add a **public hostname** on that tunnel:
   - Subdomain/domain: your `PSE_DOMAIN`
   - Service type: **HTTP**, URL: **`app:8000`**

   HTTP, not HTTPS — that hop runs inside the compose network. TLS is terminated at
   Cloudflare's edge, which is why no certificate or ACME appears anywhere in this file.
3. Fill in `.env` beside the compose file:

   ```bash
   PSE_DOMAIN=mcp.example.com
   CLOUDFLARE_TUNNEL_TOKEN=<the token from step 1>
   POSTGRES_PASSWORD=<long random value>
   ZEPTOMAIL_API_KEY=<key>
   PSE_IMAGE_TAG=0.6.0        # pin a version; see the warning below
   ```

4. `docker compose -f compose.nas.yaml up -d`, then check
   `https://$PSE_DOMAIN/.well-known/oauth-protected-resource` from anywhere.

## Pin the image tag

**`:latest` is whatever was released last, which may predate a feature this compose file
uses.** That is not hypothetical: an earlier version of this file used `:latest` while the
newest release lacked `--workers`, and the container crash-looped with
`unrecognized arguments: --workers`. Pin `PSE_IMAGE_TAG` to a version you have read the
changelog for, and bump it deliberately.

## Things worth knowing

**Do not put Cloudflare Access in front of this hostname.** Access requires a browser login,
and an MCP client cannot complete one — it will see an Access redirect where it expected
JSON. This server does its own authentication (OAuth 2.1 with passkeys); Access on top would
break every client while adding nothing you do not already have.

**`/health` and `/health/ready` are reachable through the tunnel.** They return status,
version and uptime only. If you would rather not publish the version, add a Cloudflare WAF
rule blocking `/health*`, or give the tunnel a path-scoped ingress rule. The container's own
healthcheck uses `127.0.0.1` and is unaffected either way.

**Workers on a NAS.** The default is 1. NAS CPUs have few cores, and each worker is a
separate process with its own quota windows, parse memo and token cache — so N workers means
a user's effective quota ceiling is N× the nominal one. Raise `PSE_WORKERS` only if you
measure CPU saturation, and raise the limits with it.

**Put `./backups` on a share you already back up.** A dump on the same disk as the database
protects you from mistakes, not from disk failure.

**Memory.** Defaults are modest for a NAS: 768 MB for the app, 1 GB for Postgres. Tune with
`APP_MEMORY_LIMIT` and `DB_MEMORY_LIMIT`.

**Architecture.** Both the app image and `cloudflared` are published for `linux/amd64` and
`linux/arm64`, so Intel and ARM NAS models are both covered without any change.

## Verifying, from outside

```bash
curl -fsS https://$PSE_DOMAIN/.well-known/oauth-protected-resource
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://$PSE_DOMAIN/mcp   # expect 401
```

The 401 is the point — it confirms auth is on, and it carries a `WWW-Authenticate` header
naming the metadata URL, which is how an MCP client discovers where to authenticate.

Then visit `https://$PSE_DOMAIN/signup` and complete a real signup. Do this once,
deliberately: it is the only way to confirm `PSE_PUBLIC_URL` and email delivery are both
right, and an origin mismatch discovered here costs minutes rather than arriving later as a
user's bug report about passkeys "just not working".
