"""PSE trading-session calendar: the clock behind the market-boundary freeze policy.

A cached value fetched at time T is valid until the first market CLOSE boundary
strictly after T. Consequences (all times Asia/Manila):

- fetched Monday 08:00 (pre-open)  -> valid until Monday 15:00
- fetched Monday 10:00 (session)   -> valid until Monday 15:00
- fetched Monday 15:01 (post-close)-> valid until Tuesday 15:00 (serves overnight,
  through Tuesday pre-open, and as "yesterday's close" during Tuesday's session)
- weekends/holidays contribute no boundaries; cache simply persists.

While the market is OPEN the client never fetches upstream: expired-but-present
entries are served stale (they are the latest EOD truth), and missing entries
raise MARKET_OPEN_NO_CACHE.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# PSE non-trading days. Seed list — refresh yearly (see docs). Config/DB can extend.
# NOTE: verify against the official PSE holiday circular each December.
PSE_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2026 (seeded from PH national holidays; confirm against PSE circular)
        date(2026, 1, 1),
        date(2026, 2, 17),  # Chinese New Year
        date(2026, 4, 2),  # Maundy Thursday
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 9),  # Araw ng Kagitingan
        date(2026, 5, 1),  # Labor Day
        date(2026, 6, 12),  # Independence Day
        date(2026, 8, 21),  # Ninoy Aquino Day
        date(2026, 8, 31),  # National Heroes Day
        date(2026, 11, 30),  # Bonifacio Day
        date(2026, 12, 8),  # Immaculate Conception
        date(2026, 12, 24),
        date(2026, 12, 25),
        date(2026, 12, 30),  # Rizal Day
        date(2026, 12, 31),
    }
)


class MarketCalendar:
    def __init__(
        self,
        tz: str = "Asia/Manila",
        open_time: time = time(9, 30),
        close_time: time = time(15, 0),
        holidays: frozenset[date] = PSE_HOLIDAYS,
    ):
        self.tz = ZoneInfo(tz)
        self.open_time = open_time
        self.close_time = close_time
        self.holidays = holidays

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def _local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def is_market_open(self, dt: datetime | None = None) -> bool:
        local = self._local(dt or self.now())
        if not self.is_trading_day(local.date()):
            return False
        return self.open_time <= local.time() < self.close_time

    def next_close(self, dt: datetime | None = None) -> datetime:
        """First market close strictly after `dt`."""
        local = self._local(dt or self.now())
        d = local.date()
        for _ in range(370):  # bounded walk; > 1 year of non-trading days is impossible
            if self.is_trading_day(d):
                close_dt = datetime.combine(d, self.close_time, tzinfo=self.tz)
                if close_dt > local:
                    return close_dt
            d += timedelta(days=1)
        raise RuntimeError("no trading day found within a year — holiday table corrupt?")

    def valid_until(self, fetched_at: datetime) -> datetime:
        """Freeze-policy expiry for a value fetched at `fetched_at`."""
        return self.next_close(fetched_at)

    def is_fresh(self, fetched_at: datetime, at: datetime | None = None) -> bool:
        return (at or self.now()) < self.valid_until(fetched_at)
