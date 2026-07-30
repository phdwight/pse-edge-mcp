"""Postgres implementation of the `Storage` protocol.

Interchangeable with `InMemoryStorage`: same two methods, same semantics, no TTL. Freshness
stays the calendar's decision (plan §5a) — this layer only remembers *when* a value was
fetched, never when it should expire.

What Postgres adds is sharing. With `InMemoryStorage` every process has its own cache, so
N replicas mean up to N upstream fetches per boundary. Backed by one database they collapse
to one, which is the point: the freeze policy exists to protect PSE Edge, and that promise
should not weaken as the deployment grows.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .cache import CacheEntry
from .db import cache_entries


class PostgresStorage:
    """Shared, durable cache. Implements `Storage` structurally."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, key: str) -> CacheEntry | None:
        stmt = select(cache_entries.c.value, cache_entries.c.fetched_at).where(
            cache_entries.c.key == key
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            return None
        return CacheEntry(value=row.value, fetched_at=row.fetched_at)

    async def set(self, key: str, entry: CacheEntry) -> None:
        """Upsert, because two callers can legitimately race on the same key.

        Single-flight collapses concurrent misses *within* a process; across replicas two
        processes can still both miss and both write. Last write wins, and since both are
        fetching the same frozen EOD value that is harmless — an INSERT would raise instead.
        """
        stmt = pg_insert(cache_entries).values(
            key=key, value=entry.value, fetched_at=entry.fetched_at
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[cache_entries.c.key],
            set_={"value": stmt.excluded.value, "fetched_at": stmt.excluded.fetched_at},
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def aclose(self) -> None:
        await self._engine.dispose()


async def check_schema(engine: AsyncEngine) -> None:
    """Fail loudly at startup if migrations have not been applied.

    Without this the first cache write dies mid-request with an opaque
    `UndefinedTableError`, which reads like an outage rather than "you forgot
    `alembic upgrade head`". Same spirit as invariant #4: be loud about drift.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT to_regclass('public.cache_entries') IS NOT NULL AS present")
        )
        present = result.scalar()
    if not present:
        raise RuntimeError(
            "DATABASE_URL is set but the schema is missing — run `alembic upgrade head` "
            "(or `docker compose run --rm app alembic upgrade head`) before starting the "
            "server. Unset DATABASE_URL to run with the in-memory cache instead."
        )
