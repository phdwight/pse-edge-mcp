from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from pse_edge_mcp.market_calendar import MarketCalendar

MNL = ZoneInfo("Asia/Manila")
cal = MarketCalendar()


def mnl(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=MNL)


# Mon 2026-07-27 ... Fri 2026-07-31 are plain trading days.


def test_trading_day_basics():
    assert cal.is_trading_day(date(2026, 7, 27))  # Monday
    assert not cal.is_trading_day(date(2026, 7, 26))  # Sunday
    assert not cal.is_trading_day(date(2026, 12, 25))  # holiday


def test_market_open_window():
    assert not cal.is_market_open(mnl(2026, 7, 27, 9, 29))
    assert cal.is_market_open(mnl(2026, 7, 27, 9, 30))
    assert cal.is_market_open(mnl(2026, 7, 27, 14, 59))
    assert not cal.is_market_open(mnl(2026, 7, 27, 15, 0))
    assert not cal.is_market_open(mnl(2026, 7, 26, 11, 0))  # Sunday


def test_pre_open_fetch_valid_through_session():
    """User spec: fetched Monday pre-open -> serves until Monday 15:00 close."""
    fetched = mnl(2026, 7, 27, 8, 0)
    assert cal.valid_until(fetched) == mnl(2026, 7, 27, 15, 0)
    assert cal.is_fresh(fetched, at=mnl(2026, 7, 27, 14, 59))
    assert not cal.is_fresh(fetched, at=mnl(2026, 7, 27, 15, 1))


def test_post_close_fetch_serves_overnight_and_next_session():
    """User spec: fetched Monday 15:01 -> next refetch happens after Tuesday close."""
    fetched = mnl(2026, 7, 27, 15, 1)
    assert cal.valid_until(fetched) == mnl(2026, 7, 28, 15, 0)
    assert cal.is_fresh(fetched, at=mnl(2026, 7, 28, 9, 0))  # Tuesday pre-open


def test_friday_close_carries_over_weekend():
    fetched = mnl(2026, 7, 31, 16, 0)  # Friday post-close
    assert cal.valid_until(fetched) == mnl(2026, 8, 3, 15, 0)  # Monday close
    assert cal.is_fresh(fetched, at=mnl(2026, 8, 1, 12, 0))  # Saturday


def test_holiday_skipped_in_boundary_walk():
    fetched = mnl(2026, 12, 24, 8, 0)  # Dec 24-25 holidays; Dec 28 is Monday
    assert cal.valid_until(fetched) == mnl(2026, 12, 28, 15, 0)
