"""Postgres implementation of the `AuthStore` protocol.

Split from `auth.py` for the same reason `archive_postgres.py` is split from
`archive.py`: this module imports SQLAlchemy, and the protocol side must stay importable
on an install without the `postgres` extra. Imported lazily, only when auth is enabled.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from .auth import TokenRecord
from .db import auth_tokens, users


class PostgresAuthStore:
    """One indexed lookup: token hash joined to its user."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def lookup(self, token_hash: str) -> TokenRecord | None:
        stmt = (
            select(
                auth_tokens.c.user_id,
                users.c.email,
                auth_tokens.c.expires_at,
                auth_tokens.c.revoked_at,
                users.c.disabled_at,
                users.c.quota_per_minute,
                users.c.quota_per_day,
            )
            .select_from(auth_tokens.join(users, auth_tokens.c.user_id == users.c.id))
            .where(auth_tokens.c.token_hash == token_hash, auth_tokens.c.kind == "access")
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            return None
        # Revocation, expiry and disablement are returned as facts, not filtered in SQL:
        # TokenService owns the judgement, so its rules are tested once, against fakes.
        return TokenRecord(
            user_id=row.user_id,
            email=row.email,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            user_disabled_at=row.disabled_at,
            quota_per_minute=row.quota_per_minute,
            quota_per_day=row.quota_per_day,
        )


async def check_auth_schema(engine: AsyncEngine) -> None:
    """Fail at startup, loudly and actionably, if the auth tables are missing.

    Same spirit as `storage_postgres.check_schema`: the alternative is the first request
    dying mid-flight on an opaque UndefinedTableError that reads like an outage.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT to_regclass('public.auth_tokens') IS NOT NULL AS present")
        )
        present = result.scalar()
    if not present:
        raise RuntimeError(
            "PSE_AUTH_REQUIRED is set but the auth schema is missing — run "
            "`alembic upgrade head` (or `docker compose run --rm migrate`) first."
        )
