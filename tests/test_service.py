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
    _, svc = make(mnl(2026, 7, 27, 11, 0))  # Monday, session running

    async def fetch():
        raise AssertionError("must never fetch while market is open")

    with pytest.raises(MarketOpenNoCacheError) as exc:
        await svc.get("k", fetch)
    assert exc.value.retry_after == mnl(2026, 7, 27, 15, 0)


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
