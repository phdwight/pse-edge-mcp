"""`pse-edge-admin` — account and token operations (plan §6 "admin operations").

Until the self-service OAuth flow ships (Phase 5 stage 2), this CLI is how accounts come
to exist: create a user, issue them a bearer token, hand the token over out-of-band. The
plaintext token is printed exactly once — only its SHA-256 is stored, so it cannot be
recovered later, only revoked and reissued.

Requires `DATABASE_URL` (or `--database-url`) and the `postgres` extra. The operational
verbs are plain functions taking an engine, so tests drive them directly against a real
Postgres without stdout-scraping.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    from sqlalchemy import func, insert, select, update
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncEngine
except ImportError:  # pragma: no cover - exercised only on installs without the extra
    print(
        "pse-edge-admin needs the postgres extra: pip install 'pse-edge-mcp[postgres]'",
        file=sys.stderr,
    )
    sys.exit(1)

from .accounts import erase, purge_usage
from .auth import generate_token, hash_token
from .db import auth_tokens, create_engine, normalise_url, oauth_clients, usage_events, users

# Interim default for CLI-issued personal tokens: 30 days. Deliberately longer than the
# ~30-minute OAuth access tokens of stage 2 — there is no refresh flow yet, and a token
# that dies mid-month with no self-service way to mint another is just an outage.
DEFAULT_TOKEN_TTL_MINUTES = 30 * 24 * 60


class AdminError(Exception):
    """Operator-facing failure; main() prints it and exits non-zero."""


def _normalise_email(raw: str) -> str:
    email = raw.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise AdminError(f"'{raw}' does not look like an email address")
    return email


async def create_user(
    engine: AsyncEngine,
    email: str,
    *,
    quota_per_minute: int | None = None,
    quota_per_day: int | None = None,
) -> str:
    """Create an account; returns its id. Fails loudly on a duplicate email."""
    email = _normalise_email(email)
    user_id = uuid.uuid4().hex
    stmt = insert(users).values(
        id=user_id,
        email=email,
        quota_per_minute=quota_per_minute,
        quota_per_day=quota_per_day,
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(stmt)
    except IntegrityError as exc:
        raise AdminError(f"a user with email {email} already exists") from exc
    return user_id


# Machine clients get a service account under a reserved, non-routable domain (.invalid is
# reserved by RFC 2606 and can never resolve), so a service account can never collide with
# or be mistaken for a person's address, and no mail can ever be sent to one.
MACHINE_EMAIL_DOMAIN = "machine.invalid"


async def create_machine_client(
    engine: AsyncEngine,
    name: str,
    *,
    quota_per_minute: int | None = None,
    quota_per_day: int | None = None,
) -> dict[str, str]:
    """Provision a headless client_credentials client. Prints its secret ONCE.

    A machine client is backed by a service *user*, which is what lets the whole bearer
    path stay identical to the browser flow: `/mcp` validates a token by joining
    `auth_tokens` to `users`, so a token with no user behind it would need a special case
    in the middleware — and a second code path through authentication is exactly where a
    revocation check goes missing later. It also means quotas, usage accounting and
    disablement apply to machine clients with no extra code.
    """
    if not name.strip():
        raise AdminError("--name is required and cannot be blank")

    from .oauth import OAuthService

    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "client"
    email = f"{slug}-{uuid.uuid4().hex[:8]}@{MACHINE_EMAIL_DOMAIN}"
    user_id = await create_user(
        engine, email, quota_per_minute=quota_per_minute, quota_per_day=quota_per_day
    )
    credentials = await OAuthService(engine).create_machine_client(name.strip(), user_id)
    return {**credentials, "service_user": email, "service_user_id": user_id}


async def revoke_machine_client(engine: AsyncEngine, client_id: str) -> bool:
    """Revoke the client, its secret, and every token it minted.

    Also disables the service account, so nothing can authenticate as it by any route.
    """
    from .oauth import OAuthService

    service = OAuthService(engine)
    clients = {c["client_id"]: c for c in await service.list_machine_clients()}
    record = clients.get(client_id)
    if record is None:
        raise AdminError(f"no machine client with client_id {client_id!r}")
    revoked = await service.revoke_machine_client(client_id)
    if record.get("service_user_id"):
        async with engine.begin() as conn:
            await conn.execute(
                update(users)
                .where(users.c.id == record["service_user_id"], users.c.disabled_at.is_(None))
                .values(disabled_at=datetime.now(UTC))
            )
    return revoked


async def list_machine_clients(engine: AsyncEngine) -> list[dict[str, Any]]:
    from .oauth import OAuthService

    return await OAuthService(engine).list_machine_clients()


async def machine_client_request_totals(engine: AsyncEngine) -> dict[str, int]:
    """Recorded requests per machine client, keyed by client_id.

    Each machine client is backed by a service user, so its usage is that user's rows in
    `usage_events`. The window is whatever retention holds — 90 days by policy — with no
    extra date filter to drift out of sync with it.
    """
    stmt = (
        select(
            oauth_clients.c.client_id,
            func.coalesce(func.sum(usage_events.c.requests), 0).label("requests"),
        )
        .select_from(
            oauth_clients.outerjoin(
                usage_events, usage_events.c.user_id == oauth_clients.c.service_user_id
            )
        )
        .where(oauth_clients.c.service_user_id.is_not(None))
        .group_by(oauth_clients.c.client_id)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return {row.client_id: int(row.requests) for row in rows}


async def delete_user_by_email(engine: AsyncEngine, email: str) -> dict[str, int]:
    """Erase an account by email — refusing machine-client service accounts.

    Erasing the service user behind a machine client would leave
    `oauth_clients.service_user_id` dangling: the client keeps its secret, is NOT revoked,
    and its next token mint violates the auth_tokens FK — a 500 at the token endpoint.
    The schema-walking erasure test cannot catch that, because it keys on columns named
    `user_id` and `service_user_id` dodges it by name. `revoke-machine-client` is the
    operation that does the whole job (client + tokens + account), so point there.
    """
    email = _normalise_email(email)
    async with engine.connect() as conn:
        target = (await conn.execute(select(users.c.id).where(users.c.email == email))).first()
    if target is None:
        raise AdminError(f"no user with email {email}")

    from .db import oauth_clients

    async with engine.connect() as conn:
        owning = (
            await conn.execute(
                select(oauth_clients.c.client_id).where(
                    oauth_clients.c.service_user_id == target.id
                )
            )
        ).first()
    if owning is not None:
        raise AdminError(
            f"{email} is the service account of machine client {owning.client_id!r} — "
            f"use `revoke-machine-client {owning.client_id}` instead, which revokes the "
            "client, its tokens, and disables this account in one step"
        )
    return await erase(engine, target.id)


async def issue_token(
    engine: AsyncEngine,
    email: str,
    *,
    ttl_minutes: int = DEFAULT_TOKEN_TTL_MINUTES,
    note: str | None = None,
) -> str:
    """Issue a bearer token for an existing, enabled user. Returns the plaintext ONCE."""
    email = _normalise_email(email)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(users.c.id, users.c.disabled_at).where(users.c.email == email)
            )
        ).first()
    if row is None:
        raise AdminError(f"no user with email {email} — create-user first")
    if row.disabled_at is not None:
        raise AdminError(f"user {email} is disabled; re-enable before issuing tokens")

    token = generate_token()
    async with engine.begin() as conn:
        await conn.execute(
            insert(auth_tokens).values(
                token_hash=hash_token(token),
                user_id=row.id,
                kind="access",
                note=note,
                expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes),
            )
        )
    return token


async def revoke_token(engine: AsyncEngine, token_plaintext: str) -> bool:
    """Revoke one token by its plaintext. True if an active token was revoked."""
    stmt = (
        update(auth_tokens)
        .where(
            auth_tokens.c.token_hash == hash_token(token_plaintext.strip()),
            auth_tokens.c.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
    return bool(result.rowcount)


async def disable_user(engine: AsyncEngine, email: str) -> int:
    """Disable an account and revoke all its active tokens. Returns tokens revoked."""
    email = _normalise_email(email)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        row = (await conn.execute(select(users.c.id).where(users.c.email == email))).first()
        if row is None:
            raise AdminError(f"no user with email {email}")
        await conn.execute(update(users).where(users.c.id == row.id).values(disabled_at=now))
        result = await conn.execute(
            update(auth_tokens)
            .where(auth_tokens.c.user_id == row.id, auth_tokens.c.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    return int(result.rowcount or 0)


async def set_quota(
    engine: AsyncEngine,
    email: str,
    *,
    per_minute: int | None | str = "keep",
    per_day: int | None | str = "keep",
) -> None:
    """Set per-user quota overrides. None clears an override back to the default."""
    email = _normalise_email(email)
    values: dict[str, Any] = {}
    if per_minute != "keep":
        values["quota_per_minute"] = per_minute
    if per_day != "keep":
        values["quota_per_day"] = per_day
    if not values:
        raise AdminError("nothing to change — pass --per-minute and/or --per-day")
    async with engine.begin() as conn:
        result = await conn.execute(update(users).where(users.c.email == email).values(**values))
    if not result.rowcount:
        raise AdminError(f"no user with email {email}")


async def list_users(engine: AsyncEngine) -> list[dict[str, Any]]:
    stmt = (
        select(
            users.c.email,
            users.c.created_at,
            users.c.disabled_at,
            users.c.quota_per_minute,
            users.c.quota_per_day,
            func.count(auth_tokens.c.token_hash)
            .filter(auth_tokens.c.revoked_at.is_(None), auth_tokens.c.expires_at > func.now())
            .label("active_tokens"),
        )
        .select_from(users.outerjoin(auth_tokens, auth_tokens.c.user_id == users.c.id))
        .group_by(users.c.id)
        .order_by(users.c.created_at)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [dict(row._mapping) for row in rows]


# --- argparse shell ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pse-edge-admin", description="Account and token administration"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres URL (defaults to DATABASE_URL from the environment)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="create an account")
    p.add_argument("email")
    p.add_argument("--quota-per-minute", type=int, default=None)
    p.add_argument("--quota-per-day", type=int, default=None)

    p = sub.add_parser("issue-token", help="issue a bearer token (plaintext shown once)")
    p.add_argument("email")
    p.add_argument("--ttl-minutes", type=int, default=DEFAULT_TOKEN_TTL_MINUTES)
    p.add_argument("--note", default=None, help="label, e.g. 'laptop'")

    p = sub.add_parser("revoke-token", help="revoke one token by its plaintext")
    p.add_argument("token")

    p = sub.add_parser("disable-user", help="disable an account and revoke its tokens")
    p.add_argument("email")

    p = sub.add_parser("set-quota", help="override per-user limits (0 clears to default)")
    p.add_argument("email")
    p.add_argument("--per-minute", type=int, default=None)
    p.add_argument("--per-day", type=int, default=None)

    sub.add_parser("list-users", help="list accounts with active-token counts")

    p = sub.add_parser("delete-user", help="ERASE an account and all its data (irreversible)")
    p.add_argument("email")
    p.add_argument(
        "--yes", action="store_true", help="confirm; without it the command refuses to run"
    )

    p = sub.add_parser(
        "purge-usage", help="delete usage rows past the retention window (cron this daily)"
    )
    p.add_argument("--retention-days", type=int, default=90)

    p = sub.add_parser(
        "create-machine-client",
        help="provision a headless client_credentials client (secret shown once)",
    )
    p.add_argument("--name", required=True, help="label, e.g. 'langgraph-app'")
    p.add_argument("--per-minute", type=int, default=None, help="quota override")
    p.add_argument("--per-day", type=int, default=None, help="quota override")

    p = sub.add_parser(
        "revoke-machine-client", help="revoke a machine client, its secret and all its tokens"
    )
    p.add_argument("client_id")

    sub.add_parser("list-machine-clients", help="list provisioned machine clients")
    return parser


async def _dispatch(engine: AsyncEngine, args: argparse.Namespace) -> None:
    if args.command == "create-user":
        user_id = await create_user(
            engine,
            args.email,
            quota_per_minute=args.quota_per_minute,
            quota_per_day=args.quota_per_day,
        )
        print(f"created {args.email} (id {user_id})")
    elif args.command == "issue-token":
        token = await issue_token(engine, args.email, ttl_minutes=args.ttl_minutes, note=args.note)
        print(token)
        print(
            "^ shown once — only its hash is stored. Send it to the user over a secure channel.",
            file=sys.stderr,
        )
    elif args.command == "revoke-token":
        revoked = await revoke_token(engine, args.token)
        print("revoked" if revoked else "no active token matched")
        if not revoked:
            sys.exit(1)
    elif args.command == "disable-user":
        count = await disable_user(engine, args.email)
        print(f"disabled {args.email}; revoked {count} active token(s)")
    elif args.command == "set-quota":
        await set_quota(
            engine,
            args.email,
            per_minute=(None if args.per_minute == 0 else args.per_minute)
            if args.per_minute is not None
            else "keep",
            per_day=(None if args.per_day == 0 else args.per_day)
            if args.per_day is not None
            else "keep",
        )
        print(f"quota updated for {args.email}")
    elif args.command == "delete-user":
        # The same erasure path the user's own account page uses — one implementation, so
        # the operator route cannot drift from the promise made on the privacy page.
        if not args.yes:
            raise AdminError("refusing to erase without --yes (this cannot be undone)")
        erased = await delete_user_by_email(engine, args.email)
        print(f"erased {args.email}: " + ", ".join(f"{k}={v}" for k, v in erased.items()))
    elif args.command == "purge-usage":
        from datetime import date as _date

        cutoff = _date.today() - timedelta(days=args.retention_days)
        purged = await purge_usage(engine, cutoff)
        print(f"purged {purged} usage row(s) older than {cutoff}")
    elif args.command == "list-users":
        for row in await list_users(engine):
            state = "disabled" if row["disabled_at"] else "active"
            qpm = row["quota_per_minute"] or "default"
            qpd = row["quota_per_day"] or "default"
            print(f"{row['email']:40} {state:9} tokens={row['active_tokens']} qpm={qpm} qpd={qpd}")
    elif args.command == "create-machine-client":
        created = await create_machine_client(
            engine, args.name, quota_per_minute=args.per_minute, quota_per_day=args.per_day
        )
        # Printed once and never recoverable — only the SHA-256 is stored. Deliberately on
        # stdout in a copy-paste shape, with the warning adjacent rather than at the top,
        # so it is still on screen when the operator looks away and back.
        print(f"client_id:     {created['client_id']}")
        print(f"client_secret: {created['client_secret']}")
        print(f"service_user:  {created['service_user']}")
        print()
        print("Store the secret now — it is not recoverable, only revocable.")
        print(f"Revoke with: pse-edge-admin revoke-machine-client {created['client_id']}")
    elif args.command == "revoke-machine-client":
        revoked = await revoke_machine_client(engine, args.client_id)
        print(
            f"revoked machine client {args.client_id} (tokens revoked, service account disabled)"
            if revoked
            else f"machine client {args.client_id} was already revoked"
        )
    elif args.command == "list-machine-clients":
        rows = await list_machine_clients(engine)
        if not rows:
            print("no machine clients provisioned")
        for row in rows:
            state = "revoked" if row["revoked_at"] else "active"
            print(f"{row['client_id']:28} {state:8} {row['client_name']}")


def main() -> None:
    args = build_parser().parse_args()
    if not args.database_url:
        raise SystemExit(
            "DATABASE_URL is not set (and --database-url was not given) — "
            "pse-edge-admin operates on the accounts database."
        )

    async def _run() -> None:
        engine = create_engine(normalise_url(args.database_url))
        try:
            await _dispatch(engine, args)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except AdminError as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()
