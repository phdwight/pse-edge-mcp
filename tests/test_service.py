"""Freeze-policy decision table, exercised against a controllable clock."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from pse_edge_mcp.market_calendar import MarketCalendar
from pse_edge_mcp.service import FreezeService

MNL = ZoneInfo("Asia/Manila")


class FrozenCalendar(MarketCalendar):
    def __init__(self, now: datetime):
        super().__init__()
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now


def mnl(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=MNL)


def make(now):
    cal = FrozenCalendar(now)
    return cal, FreezeService(calendar=cal)


async def test_closed_miss_fetches_then_serves_cached():
    cal, svc = make(mnl(2026, 7, 27, 8, 0))  # Monday pre-open
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"price": 100}

    first = await svc.get("k", fetch)
    assert first.meta.from_cache is False and calls == 1

    cal.set(mnl(2026, 7, 27, 11, 0))  # market now open
    second = await svc.get("k", fetch)
    assert second.meta.from_cache is True and second.meta.stale is False
    assert calls == 1  # no second upstream hit


async def test_open_price_miss_fetches_once_and_is_labelled_not_realtime():
    """EOD-frozen with nothing cached during a session: there is no previous value to
    serve, so the freeze makes its one exception — a single fetch, honestly labelled.
    The label rides on the ENTRY (derived from fetched_at), so every hit for the rest
    of the session carries it, and the first ask after the close refetches the settled
    figures and drops it."""
    cal, svc = make(mnl(2026, 7, 27, 11, 0))  # Monday, session running
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"price": 100 + calls}

    first = await svc.get("k", fetch)
    assert calls == 1 and first.meta.from_cache is False
    assert first.meta.stale is True, "a mid-session snapshot is never a settled value"
    assert first.meta.note and "realtime" in first.meta.note
    assert first.meta.valid_until == mnl(2026, 7, 27, 15, 0)

    second = await svc.get("k", fetch)
    assert calls == 1, "the session fetch happens exactly once per key"
    assert second.meta.from_cache is True
    assert second.meta.stale is True and second.meta.note, "the label outlives the fetch"

    cal.set(mnl(2026, 7, 27, 15, 1))
    settled = await svc.get("k", fetch)
    assert calls == 2, "the first ask after the close replaces the snapshot"
    assert settled.meta.stale is False and settled.meta.note is None


async def test_daily_refresh_miss_fetches_during_open_market_then_serves_from_cache():
    """Non-price data: the first ask may hit PSE Edge at any hour; every repeat of the
    same query is answered from storage until the next close boundary."""
    _, svc = make(mnl(2026, 7, 27, 11, 0))  # session running
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"v": "intraday"}

    first = await svc.get("k", fetch, policy="daily-refresh")
    assert first.meta.from_cache is False and first.meta.data_policy == "daily-refresh"
    assert first.meta.valid_until == mnl(2026, 7, 27, 15, 0)

    second = await svc.get("k", fetch, policy="daily-refresh")
    assert second.meta.from_cache is True and second.meta.stale is False
    assert calls == 1, "PSE Edge is hit once; the repeat is served from storage"


async def test_daily_refresh_expired_entry_refetches_even_during_open_market():
    """Unlike EOD-frozen, expiry during a session refreshes instead of serving stale."""
    cal, svc = make(mnl(2026, 7, 27, 10, 0))
    from pse_edge_mcp.cache import CacheEntry

    await svc.storage.set("k", CacheEntry(value={"v": "friday"}, fetched_at=mnl(2026, 7, 24, 8, 0)))

    served = await svc.get("k", lambda: _value({"v": "monday"}), policy="daily-refresh")
    assert served.value == {"v": "monday"} and served.meta.from_cache is False


async def _value(v):
    return v


async def test_post_close_query_refetches():
    """User spec: query at 15:01 fetches anew and serves until next boundary."""
    cal, svc = make(mnl(2026, 7, 27, 8, 0))
    values = iter([{"v": "pre-open"}, {"v": "eod"}])

    async def fetch():
        return next(values)

    await svc.get("k", fetch)
    cal.set(mnl(2026, 7, 27, 15, 1))
    refreshed = await svc.get("k", fetch)
    assert refreshed.value == {"v": "eod"} and refreshed.meta.from_cache is False
    assert refreshed.meta.valid_until == mnl(2026, 7, 28, 15, 0)


async def test_expired_during_open_serves_stale_never_fetches():
    cal, svc = make(mnl(2026, 7, 27, 10, 0))
    # Seed cache as if fetched pre-open FRIDAY (expired since Friday close).
    from pse_edge_mcp.cache import CacheEntry

    await svc.storage.set("k", CacheEntry(value={"v": "friday"}, fetched_at=mnl(2026, 7, 24, 8, 0)))

    async def fetch():
        raise AssertionError("must never fetch while market is open")

    served = await svc.get("k", fetch)
    assert served.value == {"v": "friday"} and served.meta.stale is True


async def test_single_flight_collapses_concurrent_misses():
    """Near-simultaneous requests for the same key: the first fetches, the rest merge
    into that in-flight request and receive its result — one upstream hit total."""
    _, svc = make(mnl(2026, 7, 27, 8, 0))
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"v": 1}

    results = await asyncio.gather(*(svc.get("k", fetch) for _ in range(10)))
    assert calls == 1
    assert all(r.value == {"v": 1} for r in results)


async def test_a_miss_that_lost_a_race_to_a_concurrent_store_does_not_refetch():
    """The gap single-flight alone cannot close: request B misses the cache, and while
    it waits its turn, request A's flight completes, stores, and is popped — so B would
    start a NEW flight and fetch the same data again (likewise a second worker writing
    to the shared Postgres cache). The flight re-checks storage before going upstream,
    turning that duplicate PSE Edge query into a cache hit."""
    cal, svc = make(mnl(2026, 7, 27, 8, 0))

    class StaleFirstRead:
        """Simulates the race: the first read reports a miss even though a concurrent
        request has just stored a fresh entry."""

        def __init__(self, inner):
            self._inner = inner
            self._first = True

        async def get(self, key):
            if self._first:
                self._first = False
                return None
            return await self._inner.get(key)

        async def set(self, key, entry):
            await self._inner.set(key, entry)

    from pse_edge_mcp.cache import CacheEntry

    await svc.storage.set(
        "k", CacheEntry(value={"v": "already-stored"}, fetched_at=mnl(2026, 7, 27, 7, 59))
    )
    svc.storage = StaleFirstRead(svc.storage)

    async def fetch():
        raise AssertionError("the answer was already stored — fetching again is the duplicate")

    served = await svc.get("k", fetch)
    assert served.value == {"v": "already-stored"}
    assert served.meta.from_cache is True and served.meta.stale is False


async def test_immutable_entry_never_refetches_across_boundaries():
    """Plan §5a: objects keyed by an immutable natural key (disclosure edge_no) are
    fetched once and never again, no matter how many close boundaries pass."""
    cal, svc = make(mnl(2026, 7, 27, 8, 0))
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"edge_no": "abc"}

    first = await svc.get("disclosure:abc", fetch, policy="immutable")
    assert first.meta.data_policy == "immutable"
    assert first.meta.valid_until is None  # no expiry to report

    cal.set(mnl(2026, 8, 14, 16, 0))  # many boundaries later
    again = await svc.get("disclosure:abc", fetch, policy="immutable")
    assert again.meta.from_cache is True and again.meta.stale is False
    assert calls == 1


async def test_immutable_miss_fetches_even_during_open_market():
    """Only price data is gated on the trading session: a never-changing object is
    fetched on first ask at any hour, then never again."""
    _, svc = make(mnl(2026, 7, 27, 11, 0))
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"edge_no": "abc"}

    served = await svc.get("disclosure:abc", fetch, policy="immutable")
    assert served.meta.from_cache is False and calls == 1

    again = await svc.get("disclosure:abc", fetch, policy="immutable")
    assert again.meta.from_cache is True and calls == 1
