"""Service layer: cache policy orchestration (market-boundary freeze).

Decision table for every read (see market_calendar for boundary semantics):

  cache state | market closed          | market open
  ------------+------------------------+---------------------------------
  fresh       | serve                  | serve
  expired     | fetch anew, cache      | serve STALE (flagged; never fetch)
  missing     | fetch anew, cache      | raise MARKET_OPEN_NO_CACHE
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .cache import CacheEntry, InMemoryStorage, Storage
from .errors import MarketOpenNoCacheError
from .market_calendar import MarketCalendar
from .models import Meta
from .ratelimit import SingleFlight


@dataclass
class Served:
    value: Any
    meta: Meta


class FreezeService:
    def __init__(self, calendar: MarketCalendar | None = None, storage: Storage | None = None):
        self.calendar = calendar or MarketCalendar()
        self.storage = storage or InMemoryStorage()
        self._flight = SingleFlight()

    async def get(self, key: str, fetch: Callable[[], Awaitable[Any]]) -> Served:
        now = self.calendar.now()
        entry = await self.storage.get(key)

        if entry is not None and self.calendar.is_fresh(entry.fetched_at, now):
            return self._served(entry, from_cache=True, stale=False)

        if self.calendar.is_market_open(now):
            if entry is not None:
                # Expired during a session: it is still the latest EOD truth.
                return self._served(entry, from_cache=True, stale=True)
            close = self.calendar.next_close(now)
            raise MarketOpenNoCacheError(
                "No cached data for this query and the market is open — upstream "
                f"fetches are frozen until the market closes at "
                f"{close.strftime('%H:%M %Z on %b %d')}. Try again after that.",
                retry_after=close,
            )

        async def _fetch_and_store() -> CacheEntry:
            value = await fetch()
            fresh = CacheEntry(value=value, fetched_at=self.calendar.now())
            await self.storage.set(key, fresh)
            return fresh

        # Concurrent misses for the same key collapse into one upstream request.
        stored = await self._flight.do(key, _fetch_and_store)
        return self._served(stored, from_cache=False, stale=False)

    def _served(self, entry: CacheEntry, *, from_cache: bool, stale: bool) -> Served:
        return Served(
            value=entry.value,
            meta=Meta(
                as_of=entry.fetched_at,
                valid_until=self.calendar.valid_until(entry.fetched_at),
                from_cache=from_cache,
                stale=stale,
            ),
        )
