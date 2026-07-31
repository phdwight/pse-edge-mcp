"""Postgres sink for the usage recorder.

Split from `usage.py` so SQLAlchemy stays off the lean-install import path, exactly as
`archive_postgres.py` and `auth_store.py` are split from their protocol modules.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .accounts import purge_usage
from .db import usage_events
from .usage import UsageBucket


class PostgresUsageSink:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def persist(self, counts: dict[UsageBucket, tuple[int, int]]) -> None:
        """Add the buffered counts onto whatever is already stored for each bucket.

        `ON CONFLICT DO UPDATE ... requests + excluded.requests` rather than a plain write,
        because several replicas flush the same user-hour bucket independently — a write
        would let the last flush erase the others' counts.
        """
        if not counts:
            return
        rows = [
            {
                "user_id": bucket.user_id,
                "day": bucket.day,
                "hour": bucket.hour,
                "requests": requests,
                "rejected": rejected,
            }
            for bucket, (requests, rejected) in counts.items()
        ]
        stmt = pg_insert(usage_events).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[usage_events.c.user_id, usage_events.c.day, usage_events.c.hour],
            set_={
                "requests": usage_events.c.requests + stmt.excluded.requests,
                "rejected": usage_events.c.rejected + stmt.excluded.rejected,
                "updated_at": func.now(),
            },
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def purge_older_than(self, cutoff: date) -> int:
        return await purge_usage(self._engine, cutoff)
