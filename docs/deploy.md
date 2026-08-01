# Deploying pse-edge-mcp

A production deployment is one command once DNS and `.env` are in place. This page covers
what to set, why the pieces are arranged as they are, and what to check afterwards.

```bash
cp .env.example .env      # then fill in the required values below
docker compose -f compose.prod.yaml up -d
```

## What this deploys

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

## Ports, and what ACME requires of them

Caddy publishes on **8280** and **8243** rather than 80 and 443, to stay clear of whatever
else already runs on the host — on a NAS, 80 and 443 usually belong to the device's own web
UI. Only the published side moves; Caddy still listens on 80/443 *inside* the container, so
the Caddyfile is untouched. Override with `PSE_HTTP_PORT` / `PSE_HTTPS_PORT`.

**Your router must forward 80 → 8280 and 443 → 8243.** This is not a preference. A
certificate authority validates by connecting to your domain on port 80 (HTTP-01) or 443
(TLS-ALPN-01); those numbers are fixed in the ACME protocol, and no Caddy setting moves them.
If the internet cannot reach this host on 80 and 443 by some path, Caddy never obtains a
certificate, and the site never serves at all — the failure is total, not degraded.

So this file suits a host you control the edge of. Where you cannot forward those ports —
CGNAT, a locked-down router, an ISP that blocks 80, or a NAS whose ports are already
spoken for — use `compose.nas.yaml` + `compose.tunnel.yaml` instead. That path needs no
inbound ports whatsoever, and Cloudflare handles the certificate.

## Required configuration

```bash
PSE_DOMAIN=mcp.example.com            # Caddy gets a certificate for this
PSE_ACME_EMAIL=ops@example.com        # CA contact address
POSTGRES_PASSWORD=<long random value>
ZEPTOMAIL_API_KEY=<key>               # verification emails; runtime only, never committed
```

**`PSE_PUBLIC_URL` must be the externally reachable https URL.** This file derives it from
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

`--workers 2` by default (`PSE_WORKERS`). Each worker is a separate process that re-imports the app, so
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
docker compose -f compose.prod.yaml ps          # all services up, app healthy
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

# headless agents (client_credentials) — see the README for the full flow
pse-edge-admin create-machine-client --name langgraph-app   # secret shown ONCE
pse-edge-admin list-machine-clients
pse-edge-admin revoke-machine-client <client_id>
```

`create-machine-client` is the **only** thing that can authorize the `client_credentials`
grant. `/oauth/register` is open to the internet, so a self-registered client is refused
that grant no matter what it declares about itself. `revoke-machine-client` clears the
secret, revokes every token the client minted, and disables its service account in one
step — the token revocation matters, or an already-issued bearer keeps working for up to
an hour.

Revocation takes effect within `PSE_TOKEN_CACHE_TTL` (60 s default). That TTL is the
revocation-latency budget and nothing else — lowering it costs database reads, raising it
lengthens the window in which a revoked token still works.

---

# NAS deployment, in two stages

Bring the stack up on the NAS first and confirm it works on your LAN; add the public
hostname afterwards. Stage 1 is a single file; stage 2 adds one alongside it.

`compose.nas.yaml` is standalone rather than an overlay — NAS Docker UIs import a single
file much more happily — and **pulls** the published image instead of building, since a NAS
is a poor build host.

**Use `compose.nas.yaml`, not `compose.prod.yaml`.** The Caddy file is for a host that owns
ports 80 and 443; on a NAS it fails twice over. It bind-mounts `Caddyfile` from beside
itself, so importing the compose file alone leaves the `caddy` container *created but never
started* with an opaque OCI "not a directory" error — and even with the file present, ACME
cannot issue a certificate unless your router forwards 80 and 443. `compose.nas.yaml` mounts
no repository files at all, so a single-file import is complete.

## "Error" in the NAS UI, with `migrate` stopped

`migrate` **runs once and exits 0**. That is success. It applies `alembic upgrade head` and
finishes — the schema must not be applied by the server on boot, because replicas would race
to mutate it (plan §5). NAS Docker UIs list any stopped container as "Not in use" and colour
the whole project red on that basis, so a healthy deployment looks broken.

Read the containers rather than the badge. This is a correct stack:

| Container | Expected |
|---|---|
| `db`, `app`, `backup`, `purge` | Running (`app` healthy) |
| `migrate` | **Exited (0)** — its log ends with `Running upgrade …` |

If `app` is running and answering `/health`, the deployment is fine whatever the project
badge says. A genuine failure looks different: `migrate` exited **non-zero**, or `app`
restarting in a loop.

## Stage 1 — LAN only

```bash
PSE_IMAGE_TAG=0.8.1            # pin a version; see the warning below
POSTGRES_PASSWORD=<long random value>
```

```bash
docker compose -f compose.nas.yaml up -d
```

That is the whole stage. No Cloudflare account, domain or token is involved yet — those
variables live in the stage 2 file, so nothing asks for them until you opt in. The server
is reachable at `http://<nas-ip>:8200`, and nothing is exposed to the internet.

Check it:

```bash
curl http://<nas-ip>:8200/health           # {"status": "ok", "version": "0.8.1", ...}
curl -X POST http://<nas-ip>:8200/mcp      # 401 — auth is on
```

**Auth is on even on the LAN**, because a NAS is a shared machine and turning auth on
afterwards would orphan any token minted while it was off. Passkey signup, though, cannot
work here: browsers restrict WebAuthn to secure contexts, and this is plain http. So mint a
token directly instead:

```bash
docker compose -f compose.nas.yaml exec app pse-edge-admin create-user you@example.com
docker compose -f compose.nas.yaml exec app pse-edge-admin issue-token you@example.com --note nas
```

```bash
curl -X POST http://<nas-ip>:8200/mcp \
  -H "Authorization: Bearer pse_..." \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Twelve tools back means the stack is sound: image, migrations, database, auth and the MCP
transport are all working. What stage 1 *cannot* tell you is whether passkeys, OAuth and
verification email work — all three need the real https origin, so they are stage 2's
checklist, not something you have deferred by accident.

## Stage 2 — public hostname via Cloudflare Tunnel

`cloudflared` dials **out** to Cloudflare, so there is no port forwarding, nothing for CGNAT
to break, no dynamic-DNS to maintain, and no contest with the NAS's own web UI over ports 80
and 443.

1. **Cloudflare Zero Trust → Networks → Tunnels → Create a tunnel** (type: Cloudflared).
   Copy the token it shows you.
2. Add a **public hostname** on that tunnel:
   - Subdomain/domain: your `PSE_DOMAIN`
   - Service type: **HTTP**, URL: **`app:8000`**

   HTTP, not HTTPS — that hop runs inside the compose network. TLS is terminated at
   Cloudflare's edge, which is why no certificate or ACME appears anywhere in this
   deployment.
3. Add to the same `.env`:

   ```bash
   PSE_DOMAIN=mcp.example.com
   CLOUDFLARE_TUNNEL_TOKEN=<the token from step 1>
   ZEPTOMAIL_API_KEY=<key>          # verification email, now that strangers can sign up
   PSE_LAN_BIND=127.0.0.1           # closes the stage 1 LAN port — see below
   ```

4. Bring it up with both files:

   ```bash
   docker compose -f compose.nas.yaml -f compose.tunnel.yaml up -d --remove-orphans
   ```

The overlay starts `cloudflared` and swaps `PSE_PUBLIC_URL` to the https hostname.

**`PSE_LAN_BIND=127.0.0.1` is what closes the stage 1 LAN port**, and it is a separate line
in `.env` rather than something the overlay does for you. Compose merges `ports` additively
— a second file can add a mapping but never remove one — so an overlay genuinely cannot take
the port away. Setting the bind address moves it to the NAS's own loopback instead: still
there for debugging from the NAS shell, no longer reachable from the local network. Confirm
it rather than assuming, from a *different* machine:

```bash
curl -sf https://mcp.example.com/health && echo "public: up"
curl -s -m 4 http://<nas-ip>:8200/health || echo "LAN port: closed (correct)"
```

Then **do one real passkey signup immediately.** It is the only check that proves
`PSE_PUBLIC_URL` matches the origin browsers see; caught now it costs a minute, caught later
it arrives as "passkeys just don't work", and enrolled credentials cannot be migrated, only
re-enrolled.

If you would rather run a single file long-term — some NAS UIs only import one — flatten the
two once the tunnel works:

```bash
docker compose -f compose.nas.yaml -f compose.tunnel.yaml config > compose.merged.yaml
```

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

## Verifying stage 2, from outside

```bash
curl -fsS https://$PSE_DOMAIN/.well-known/oauth-protected-resource
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://$PSE_DOMAIN/mcp   # expect 401
```

The 401 is the point — it confirms auth is on, and it carries a `WWW-Authenticate` header
naming the metadata URL, which is how an MCP client discovers where to authenticate. Check
that the `resource` field in the metadata is your real hostname and not `localhost`: that is
`PSE_PUBLIC_URL` reflected back, and it is what clients will try to authenticate against.

Then visit `https://$PSE_DOMAIN/signup` and complete a real signup, as above — the one check
stage 1 could not perform.
