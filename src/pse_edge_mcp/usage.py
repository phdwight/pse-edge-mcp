"""Per-user usage accounting with capped retention (plan §6, §6a).

Two obligations meet here. §6 wants a per-user usage audit log; §6a caps its retention and
insists on minimal data collection. Both are served by recording **aggregated counts per
user-hour** rather than a row per request:

- It answers the questions a log exists for — what did this account do, and roughly when —
  without retaining a per-request trail of someone's activity. Holding less is the
  compliance goal, not a shortcut around it.
- It keeps the hot path free of database writes, the same rule quotas follow. A row per
  request would be ~86M rows/day at 1k req/s.

Counts accumulate in memory and flush on an interval, so a request never waits on the
database. The cost of that choice, stated plainly: **a crash loses at most one flush
interval of counts**. That is acceptable for an abuse-and-transparency log and would not be
for billing — if this ever bills, revisit it.

This module must not import SQLAlchemy: it sits on the import path of installs without the
`postgres` extra. The sink that talks to Postgres lives in `usage_postgres.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_SECONDS = 30.0
DEFAULT_RETENTION_DAYS = 90


@dataclass(frozen=True)
class UsageBucket:
    user_id: str
    day: date
    hour: int


class UsageSink(Protocol):
    async def persist(self, counts: dict[UsageBucket, tuple[int, int]]) -> None: ...

    async def purge_older_than(self, cutoff: date) -> int: ...


class NullUsageRecorder:
    """No-op default: stdio keeps no account and therefore no usage log."""

    def record(self, user_id: str, *, rejected: bool = False) -> None:
        return None

    async def flush(self) -> None:
        return None


class UsageRecorder:
    """Buffers counts in memory and flushes them to a sink on an interval."""

    def __init__(
        self,
        sink: UsageSink,
        *,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._sink = sink
        self._flush_seconds = flush_seconds
        self._retention_days = retention_days
        self._now = now or (lambda: datetime.now(UTC))
        self._counts: dict[UsageBucket, list[int]] = {}
        self._last_flush = 0.0
        self._last_purge: date | None = None
        self._task: asyncio.Task[None] | None = None

    def record(self, user_id: str, *, rejected: bool = False) -> None:
        """Count one request. Synchronous and allocation-light — this is the hot path."""
        moment = self._now()
        bucket = UsageBucket(user_id=user_id, day=moment.date(), hour=moment.hour)
        entry = self._counts.get(bucket)
        if entry is None:
            entry = [0, 0]
            self._counts[bucket] = entry
        entry[0] += 1
        if rejected:
            entry[1] += 1

    async def flush(self) -> None:
        """Persist and clear the buffer, then purge expired rows at most once a day.

        Failures are logged, not raised: usage accounting is bookkeeping riding along on
        real requests, and losing a flush must never break them — the same rule the
        archive follows. The buffer is swapped out *before* the write so a slow or failing
        sink cannot block new counts from accumulating.
        """
        pending, self._counts = self._counts, {}
        if pending:
            try:
                await self._sink.persist({k: (v[0], v[1]) for k, v in pending.items()})
            except Exception:
                logger.warning(
                    "usage flush failed; %d buckets dropped", len(pending), exc_info=True
                )

        today = self._now().date()
        if self._last_purge != today:
            self._last_purge = today
            cutoff = today - timedelta(days=self._retention_days)
            try:
                removed = await self._sink.purge_older_than(cutoff)
                if removed:
                    logger.info("purged %d usage rows older than %s", removed, cutoff)
            except Exception:
                logger.warning("usage retention purge failed", exc_info=True)

    async def run_forever(self) -> None:
        """Flush loop, started with the app and cancelled with it."""
        while True:
            await asyncio.sleep(self._flush_seconds)
            await self.flush()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        """Cancel the loop and flush what is still buffered, so shutdown loses nothing."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()
