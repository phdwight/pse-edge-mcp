"""Account erasure and the data an account can see about itself (plan §6a).

Collecting an email address makes the operator a personal-information controller under the
PH Data Privacy Act, and under GDPR for foreign users. Two obligations follow that this
module implements: a subject can **see** what is held about them, and can **erase** it
themselves without asking anyone.

Erasure is a hard delete of every table keyed to the user, in foreign-key-safe order, in
one transaction — not a `disabled_at` flag. A soft delete would leave the email address on
file, which is the opposite of what erasure means, and would quietly make the promise on
the privacy page false.

What deliberately survives, and why it is not personal data: rows in `eod_bars` and
`disclosures` are public PSE Edge market facts that were never about the user, and
`oauth_clients` describes a piece of software, not a person.

`auth_tokens` rows carry only a SHA-256 hash and a user id, but they are keyed to the user
and go too — otherwise a deleted account's tokens would linger as orphans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from .db import (
    auth_tokens,
    oauth_flows,
    usage_events,
    users,
    web_sessions,
    webauthn_credentials,
)


class AccountError(Exception):
    """User-facing account operation failure."""


@dataclass(frozen=True)
class AccountSummary:
    """Everything the service holds about one account, for the subject to inspect."""

    email: str
    created_at: Any
    passkeys: int
    active_tokens: int
    usage_days: list[dict[str, Any]]


async def summarise(engine: AsyncEngine, user_id: str) -> AccountSummary:
    """The subject-access view: what we hold, in one place, in plain terms."""
    async with engine.connect() as conn:
        account = (
            await conn.execute(
                select(users.c.email, users.c.created_at).where(users.c.id == user_id)
            )
        ).first()
        if account is None:
            raise AccountError("account not found")

        passkeys = (
            await conn.execute(
                select(func.count())
                .select_from(webauthn_credentials)
                .where(webauthn_credentials.c.user_id == user_id)
            )
        ).scalar_one()
        # "Active" means usable right now: unrevoked *and* unexpired. Expired rows
        # linger until a mint purges them, and counting those would show a user four
        # "active tokens" for one connected client.
        tokens = (
            await conn.execute(
                select(func.count())
                .select_from(auth_tokens)
                .where(
                    auth_tokens.c.user_id == user_id,
                    auth_tokens.c.revoked_at.is_(None),
                    auth_tokens.c.expires_at > func.now(),
                )
            )
        ).scalar_one()
        usage = (
            await conn.execute(
                select(
                    usage_events.c.day,
                    func.sum(usage_events.c.requests).label("requests"),
                    func.sum(usage_events.c.rejected).label("rejected"),
                )
                .where(usage_events.c.user_id == user_id)
                .group_by(usage_events.c.day)
                .order_by(usage_events.c.day.desc())
            )
        ).all()

    return AccountSummary(
        email=account.email,
        created_at=account.created_at,
        passkeys=int(passkeys),
        active_tokens=int(tokens),
        usage_days=[dict(row._mapping) for row in usage],
    )


async def erase(engine: AsyncEngine, user_id: str) -> dict[str, int]:
    """Delete the account and everything keyed to it. Returns rows removed per table.

    One transaction: a partial erasure — say, the account gone but its tokens left behind —
    would be both a live credential and a broken promise.
    """
    removed: dict[str, int] = {}
    async with engine.begin() as conn:
        exists = (await conn.execute(select(users.c.id).where(users.c.id == user_id))).first()
        if exists is None:
            raise AccountError("account not found")

        # Children first: every table that references users.id, then the user.
        for name, table, column in (
            ("usage_events", usage_events, usage_events.c.user_id),
            ("auth_tokens", auth_tokens, auth_tokens.c.user_id),
            ("webauthn_credentials", webauthn_credentials, webauthn_credentials.c.user_id),
            ("web_sessions", web_sessions, web_sessions.c.user_id),
            # oauth_flows.user_id has no FK constraint but still names the person.
            ("oauth_flows", oauth_flows, oauth_flows.c.user_id),
        ):
            result = await conn.execute(delete(table).where(column == user_id))
            removed[name] = int(result.rowcount or 0)

        result = await conn.execute(delete(users).where(users.c.id == user_id))
        removed["users"] = int(result.rowcount or 0)
    return removed


async def purge_usage(engine: AsyncEngine, cutoff: date) -> int:
    """Drop usage rows older than `cutoff`. Used by the recorder and the admin CLI."""
    async with engine.begin() as conn:
        result = await conn.execute(delete(usage_events).where(usage_events.c.day < cutoff))
    return int(result.rowcount or 0)
