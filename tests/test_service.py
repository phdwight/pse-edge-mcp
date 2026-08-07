"""Freeze-policy decision table, exercised against a controllable clock."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pse_edge_mcp.errors import MarketOpenNoCacheError
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


async def test_open_miss_refuses_with_retry_after():
    """EOD-frozen (price data) — and deliberately also the DEFAULT, so a caller that
    forgets to name a policy gets the freeze, never an accidental intraday fetch."""
    _, svc = make(mnl(2026, 7, 27, 11, 0))  # Monday, session running

    async def fetch():
        raise AssertionError("price data must never fetch while market is open")

    with pytest.raises(MarketOpenNoCacheError) as exc:
        await svc.get("k", fetch)
    assert exc.value.retry_after == mnl(2026, 7, 27, 15, 0)


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
