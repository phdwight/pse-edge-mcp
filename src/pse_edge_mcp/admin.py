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
from .db import auth_tokens, create_engine, normalise_url, users

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
            .filter(auth_tokens.c.revoked_at.is_(None))
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
        async with engine.connect() as conn:
            target = (
                await conn.execute(select(users.c.id).where(users.c.email == args.email.lower()))
            ).first()
        if target is None:
            raise AdminError(f"no user with email {args.email}")
        if not args.yes:
            raise AdminError("refusing to erase without --yes (this cannot be undone)")
        erased = await erase(engine, target.id)
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
