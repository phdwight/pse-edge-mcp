"""Service layer: cache policy orchestration.

Each read names its policy (the repository chooses it per domain):

- **EOD-frozen** — price data only. The market-boundary freeze applies in full:

    cache state | market closed          | market open
    ------------+------------------------+---------------------------------
    fresh       | serve                  | serve
    expired     | fetch anew, cache      | serve STALE (flagged; never fetch)
    missing     | fetch anew, cache      | raise MARKET_OPEN_NO_CACHE

- **daily-refresh** — everything else (disclosures, profiles, financials, indices…).
  A miss or expiry fetches at ANY hour, so PSE Edge is hit once per unique query per
  boundary window; every repeat is served from storage until the next market close.

- **immutable** — objects that never change upstream (a disclosure by edge_no).
  Fetched once, at any hour, never refetched.

If the fetch itself fails because PSE Edge is unreachable, an expired entry is served
STALE rather than discarded: holding the last close and answering with an error instead
is strictly worse, and `stale` already means "real data, past its boundary".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .cache import CacheEntry, InMemoryStorage, Storage
from .errors import EdgeUnavailableError, MarketOpenNoCacheError
from .market_calendar import MarketCalendar
from .models import DataPolicy, Meta
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
        self, key: str, fetch: Callable[[], Awaitable[Any]], *, policy: DataPolicy = "EOD-frozen"
    ) -> Served[Any]: ...


class FreezeService:
    def __init__(self, calendar: MarketCalendar | None = None, storage: Storage | None = None):
        self.calendar = calendar or MarketCalendar()
        self.storage = storage or InMemoryStorage()
        self._flight = SingleFlight()

    async def get(
        self, key: str, fetch: Callable[[], Awaitable[Any]], *, policy: DataPolicy = "EOD-frozen"
    ) -> Served[Any]:
        """The default is the strictest policy on purpose: a caller that forgets to name
        one gets the full market-boundary freeze, never an accidental intraday fetch.
        `immutable` marks objects that never change upstream (plan §5a) — a disclosure
        keyed by edge_no, for instance. Once cached they are never refetched.
        """
        now = self.calendar.now()
        entry = await self.storage.get(key)

        if entry is not None and (
            policy == "immutable" or self.calendar.is_fresh(entry.fetched_at, now)
        ):
            return self._served(entry, from_cache=True, stale=False, policy=policy)

        # Only price data is gated on the trading session. Everything else may fetch at
        # any hour — once — and then answers from storage until the next close boundary.
        if policy == "EOD-frozen" and self.calendar.is_market_open(now):
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
            # every one is worth a log entry. A policy=EOD-frozen fetch appearing during
            # market hours means the freeze invariant is broken; other policies fetch at
            # any hour but at most once per key per boundary window.
            started = time.perf_counter()
            logger.info("upstream: fetching from PSE Edge key=%s policy=%s", key, policy)
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
        try:
            stored = await self._flight.do(key, _fetch_and_store)
        except EdgeUnavailableError:
            if entry is None:
                raise  # Nothing cached and nothing upstream: there is no answer to give.
            # We hold the last close and PSE Edge is unreachable. Throwing that away to
            # return an error is strictly worse than serving it: the data is real, it is
            # simply past its boundary — which is exactly what `stale` already means, so a
            # client that handles `stale` handles an outage with no change. `as_of` says
            # precisely how old it is, and callers already have to read it.
            logger.warning(
                "upstream: PSE Edge unreachable — serving the cached value as stale "
                "key=%s as_of=%s",
                key,
                entry.fetched_at.isoformat(),
            )
            return self._served(entry, from_cache=True, stale=True, policy=policy)
        return self._served(stored, from_cache=False, stale=False, policy=policy)

    def _served(
        self,
        entry: CacheEntry,
        *,
        from_cache: bool,
        stale: bool,
        policy: DataPolicy = "EOD-frozen",
    ) -> Served[Any]:
        immutable = policy == "immutable"
        return Served(
            value=entry.value,
            meta=Meta(
                as_of=entry.fetched_at,
                valid_until=None if immutable else self.calendar.valid_until(entry.fetched_at),
                from_cache=from_cache,
                data_policy=policy,
                stale=stale,
            ),
        )
