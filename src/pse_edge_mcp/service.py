"""Service layer: cache policy orchestration.

Each read names its policy (the repository chooses it per domain):

- **EOD-frozen** — price data only. The market-boundary freeze applies in full:

    cache state | market closed          | market open
    ------------+------------------------+----------------------------------------
    fresh       | serve                  | serve
    expired     | fetch anew, cache      | serve STALE (flagged; never fetch)
    missing     | fetch anew, cache      | fetch ONCE, serve flagged NOT REALTIME

  A price snapshot taken mid-session is never presented as settled: it serves with
  `stale: true` and an explanatory `note` for the rest of the session (the label is
  derived from `fetched_at`, so it outlives the request that fetched it), and the
  first ask after the close replaces it with the real end-of-day figures.

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
from .errors import EdgeUnavailableError
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
    INTRADAY_NOTE = (
        "Not a realtime value: this price was fetched during the trading session because "
        "nothing was cached. PSE Edge session data is delayed and is not the settled "
        "end-of-day figure; it refreshes after the 15:00 Manila close."
    )

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
                # Expired during a session: it is still the latest EOD truth, and
                # strictly better than a mid-session snapshot — never refetched.
                return self._served(entry, from_cache=True, stale=True)
            # Nothing cached at all: there is no previous value to serve, so the one
            # exception to the freeze (decided 2026-08-07) — fetch once, single-flighted,
            # and label the result as not realtime. _served derives that label from
            # fetched_at, so every repeat this session carries it too, and the first ask
            # after the close replaces the snapshot with the settled EOD figures.
            logger.info(
                "freeze: uncached price read during the session — fetching once, "
                "serving as non-realtime key=%s",
                key,
            )

        async def _fetch_and_store() -> tuple[CacheEntry, bool]:
            # Re-check storage now that we hold the flight. Between this request's cache
            # miss and its turn here, a concurrent request may already have fetched and
            # stored the answer — either one whose flight completed in the gap (its
            # future is popped once done, so we would start a NEW flight), or another
            # worker process writing to the shared Postgres cache. Fetching again would
            # be exactly the duplicate upstream query this path exists to prevent.
            current = await self.storage.get(key)
            if current is not None and (
                policy == "immutable"
                or self.calendar.is_fresh(current.fetched_at, self.calendar.now())
            ):
                return current, False

            # THE line to watch. This server exists to keep upstream requests rare, so
            # every one is worth a log entry. Every policy fetches at most once per key
            # per boundary window; a policy=EOD-frozen fetch during market hours is
            # legitimate only for a key with nothing cached (the non-realtime fallback) —
            # repeats of the same key within one session mean the freeze is broken.
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
            return fresh, True

        # Concurrent misses for the same key collapse into one upstream request: the
        # first caller runs the fetch, every simultaneous caller awaits the same future
        # and receives the same result (see SingleFlight).
        try:
            stored, fetched = await self._flight.do(key, _fetch_and_store)
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
        return self._served(stored, from_cache=not fetched, stale=False, policy=policy)

    def _served(
        self,
        entry: CacheEntry,
        *,
        from_cache: bool,
        stale: bool,
        policy: DataPolicy = "EOD-frozen",
    ) -> Served[Any]:
        immutable = policy == "immutable"
        note = None
        if policy == "EOD-frozen" and self.calendar.is_market_open(entry.fetched_at):
            # A price snapshot taken mid-session is never a settled EOD value, no matter
            # when or from where it is served. Deriving the label from fetched_at makes
            # it outlive the request that fetched it: cache hits for the rest of the
            # session carry the same flag, and it disappears only when the post-close
            # refetch replaces the entry.
            stale = True
            note = self.INTRADAY_NOTE
        return Served(
            value=entry.value,
            meta=Meta(
                as_of=entry.fetched_at,
                valid_until=None if immutable else self.calendar.valid_until(entry.fetched_at),
                from_cache=from_cache,
                data_policy=policy,
                stale=stale,
                note=note,
            ),
        )
