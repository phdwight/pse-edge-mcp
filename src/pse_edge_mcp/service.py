"""Service layer: cache policy orchestration (market-boundary freeze).

Decision table for every read (see market_calendar for boundary semantics):

  cache state | market closed          | market open
  ------------+------------------------+---------------------------------
  fresh       | serve                  | serve
  expired     | fetch anew, cache      | serve STALE (flagged; never fetch)
  missing     | fetch anew, cache      | raise MARKET_OPEN_NO_CACHE
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .cache import CacheEntry, InMemoryStorage, Storage
from .errors import MarketOpenNoCacheError
from .market_calendar import MarketCalendar
from .models import Meta
from .ratelimit import SingleFlight

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Served[T]:
    """A value plus the freshness metadata that must travel with it.

    Generic so the layers above can carry their own types (`Served[StockQuote]`)
    while the cache itself stays type-agnostic (`Served[Any]`).
    """

    value: T
    meta: Meta

    def map[U](self, transform: Callable[[T], U]) -> Served[U]:
        """Re-type the payload while preserving the metadata unchanged.

        Callers parse or validate `value` constantly and must not have to remember to
        carry `meta` across by hand — forgetting it is how freshness information gets
        silently dropped from a tool result.
        """
        return Served(value=transform(self.value), meta=self.meta)


class FrozenCache(Protocol):
    """What the domain layer needs from the freeze policy — nothing more.

    Depending on this rather than on `FreezeService` keeps repositories testable with a
    trivial stub and leaves the policy free to change (Postgres-backed, different
    calendar) without touching them.
    """

    async def get(
        self, key: str, fetch: Callable[[], Awaitable[Any]], *, immutable: bool = False
    ) -> Served[Any]: ...


class FreezeService:
    def __init__(self, calendar: MarketCalendar | None = None, storage: Storage | None = None):
        self.calendar = calendar or MarketCalendar()
        self.storage = storage or InMemoryStorage()
        self._flight = SingleFlight()

    async def get(
        self, key: str, fetch: Callable[[], Awaitable[Any]], *, immutable: bool = False
    ) -> Served[Any]:
        """`immutable=True` marks objects that never change upstream (plan §5a) — a
        disclosure keyed by edge_no, for instance. Once cached they are never refetched
        at any boundary. The open-market freeze still applies to the *first* fetch:
        protecting PSE Edge outranks serving a cache miss promptly.
        """
        now = self.calendar.now()
        entry = await self.storage.get(key)

        if entry is not None and (immutable or self.calendar.is_fresh(entry.fetched_at, now)):
            return self._served(entry, from_cache=True, stale=False, immutable=immutable)

        if self.calendar.is_market_open(now):
            if entry is not None:
                # Expired during a session: it is still the latest EOD truth.
                return self._served(entry, from_cache=True, stale=True)
            close = self.calendar.next_close(now)
            logger.info(
                "freeze: refusing an uncached read while the market is open key=%s retry_after=%s",
                key,
                close.isoformat(),
            )
            raise MarketOpenNoCacheError(
                "No cached data for this query and the market is open — upstream "
                f"fetches are frozen until the market closes at "
                f"{close.strftime('%H:%M %Z on %b %d')}. Try again after that.",
                retry_after=close,
            )

        async def _fetch_and_store() -> CacheEntry:
            # THE line to watch. This server exists to keep upstream requests rare, so
            # every one is worth a log entry: they should be a trickle after each close,
            # and one appearing during market hours means the freeze invariant is broken.
            started = time.perf_counter()
            logger.info("upstream: fetching from PSE Edge key=%s", key)
            value = await fetch()
            fresh = CacheEntry(value=value, fetched_at=self.calendar.now())
            await self.storage.set(key, fresh)
            logger.info(
                "upstream: fetched and cached key=%s duration_ms=%d",
                key,
                int((time.perf_counter() - started) * 1000),
            )
            return fresh

        # Concurrent misses for the same key collapse into one upstream request.
        stored = await self._flight.do(key, _fetch_and_store)
        return self._served(stored, from_cache=False, stale=False, immutable=immutable)

    def _served(
        self, entry: CacheEntry, *, from_cache: bool, stale: bool, immutable: bool = False
    ) -> Served[Any]:
        return Served(
            value=entry.value,
            meta=Meta(
                as_of=entry.fetched_at,
                valid_until=None if immutable else self.calendar.valid_until(entry.fetched_at),
                from_cache=from_cache,
                data_policy="immutable" if immutable else "EOD-frozen",
                stale=stale,
            ),
        )
