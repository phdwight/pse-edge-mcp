"""The nightly schema canary, and the outage fallback it shares a purpose with.

Both exist for the same reason: PSE Edge can change or vanish without warning, and the
question is only whether the operator finds out before a user does.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pse_edge_mcp.cache import CacheEntry, InMemoryStorage
from pse_edge_mcp.canary import PROBE_SYMBOL, run_and_notify, run_canary
from pse_edge_mcp.config import Settings
from pse_edge_mcp.errors import EdgeUnavailableError
from pse_edge_mcp.market_calendar import MarketCalendar
from pse_edge_mcp.service import FreezeService

MNL = ZoneInfo("Asia/Manila")
CLOSED = datetime(2026, 7, 30, 16, 30, tzinfo=MNL)  # Thursday, after the close
FIXTURES = Path(__file__).parent / "fixtures"


class ClosedMarket(MarketCalendar):
    def now(self) -> datetime:
        return CLOSED

    def is_market_open(self, dt: datetime | None = None) -> bool:
        return False


class OpenMarket(ClosedMarket):
    def is_market_open(self, dt: datetime | None = None) -> bool:
        return True


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeEdge:
    """PSE Edge replaying the recorded fixtures — i.e. the shape we last verified."""

    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls: list[str] = []

    def _answer(self, name: str, default):
        self.calls.append(name)
        if name in self.overrides:
            value = self.overrides[name]
            if isinstance(value, Exception):
                raise value
            return value
        return default

    async def search_companies(self, query: str):
        return self._answer(
            "search_companies",
            [{"cmpyId": "599", "cmpyNm": "SM Investments", "symbol": PROBE_SYMBOL, "etfYn": "0"}],
        )

    async def fetch_stock_data_page(self, company_id: str) -> str:
        return self._answer("stock_data", fixture("stock_data.html"))

    async def fetch_price_history(self, company_id, security_id, start, end):
        # The recorded shape, not a hand-rolled one: a fake that drifts from the real
        # payload tests the fake rather than the canary.
        return self._answer("chart", json.loads(fixture("chart.json")))

    async def search_announcements(self, **kwargs) -> str:
        return self._answer("announcements", fixture("announcements_search.html"))

    async def fetch_company_information(self, company_id: str) -> str:
        return self._answer("company_profile", fixture("company_profile.html"))

    async def fetch_financial_reports(self, company_id: str) -> str:
        return self._answer("financials", fixture("financial_reports.html"))

    async def fetch_dividends_or_rights(self, company_id: str, kind: str) -> str:
        return self._answer("dividends", fixture("dividends.html"))

    async def fetch_homepage(self) -> str:
        return self._answer("homepage", fixture("homepage.html"))

    async def aclose(self) -> None:
        pass


SETTINGS = Settings(throttle_rate_per_sec=1000)


async def test_a_healthy_edge_passes_every_family():
    report = await run_canary(SETTINGS, calendar=ClosedMarket(), client=FakeEdge())

    assert report.ok, report.as_text()
    assert len(report.checks) >= 7, "one check per endpoint family"
    assert "healthy" in report.summary()


async def test_the_canary_refuses_to_run_while_the_market_is_open():
    edge = FakeEdge()
    report = await run_canary(SETTINGS, calendar=OpenMarket(), client=edge)

    assert report.skipped, "invariant #1 outranks knowing about drift promptly"
    assert edge.calls == [], "not one upstream request during a session"
    assert report.ok, "a skip is not a failure"


async def test_restyled_html_is_caught_even_though_http_says_200():
    """The failure this exists for: Edge serves a perfectly good 200 with a changed table,
    which is invisible at the HTTP layer and only shows up when something parses it."""
    edge = FakeEdge(company_profile="<html><body><p>redesigned</p></body></html>")

    report = await run_canary(SETTINGS, calendar=ClosedMarket(), client=edge)

    assert not report.ok
    names = [c.name for c in report.failures]
    assert any("company_profile" in n or "CompanyProfile" in n for n in names), names
    # One family breaking must not mask the others.
    assert len(report.checks) >= 7
    assert len(report.failures) == 1


async def test_an_unreachable_edge_is_reported_rather_than_raised():
    edge = FakeEdge(search_companies=EdgeUnavailableError("PSE Edge unreachable"))

    report = await run_canary(SETTINGS, calendar=ClosedMarket(), client=edge)

    assert not report.ok
    assert "EdgeUnavailableError" in report.as_text()


async def test_failures_email_the_operator_and_successes_stay_silent():
    """A nightly 'all fine' message is filtered within a week, and a filtered alert is
    worse than none because it feels like coverage."""

    class Mailbox:
        def __init__(self):
            self.sent = []

        async def send(self, *, to, subject, html):
            self.sent.append((to, subject, html))

    healthy, broken = Mailbox(), Mailbox()
    settings = Settings(throttle_rate_per_sec=1000, operator_email="ops@example.com")

    import pse_edge_mcp.canary as canary_module

    async def fake_run(_settings):
        return await run_canary(settings, calendar=ClosedMarket(), client=FakeEdge())

    async def fake_run_broken(_settings):
        return await run_canary(
            settings, calendar=ClosedMarket(), client=FakeEdge(homepage="<html></html>")
        )

    original = canary_module.run_canary
    try:
        canary_module.run_canary = fake_run
        await run_and_notify(settings, sender=healthy)
        canary_module.run_canary = fake_run_broken
        await run_and_notify(settings, sender=broken)
    finally:
        canary_module.run_canary = original

    assert healthy.sent == [], "silent on success"
    assert len(broken.sent) == 1
    to, subject, body = broken.sent[0]
    assert to == "ops@example.com"
    assert "FAILED" in subject
    assert "docs/endpoints.md" in body, "the mail should say what to do about it"


# --- outage resilience -------------------------------------------------------


async def dead_upstream():
    raise EdgeUnavailableError("PSE Edge unreachable: All connection attempts failed")


async def test_an_outage_serves_the_last_close_flagged_stale():
    """Holding yesterday's close and answering with an error instead is strictly worse.
    `stale` already means 'real data, past its boundary', so a client that handles the
    market-open case handles an outage with no change."""
    storage = InMemoryStorage()
    await storage.set(
        "quote:SM", CacheEntry(value={"close": 620.0}, fetched_at=CLOSED - timedelta(days=2))
    )
    service = FreezeService(calendar=ClosedMarket(), storage=storage)

    served = await service.get("quote:SM", dead_upstream)

    assert served.value == {"close": 620.0}
    assert served.meta.stale is True
    assert served.meta.from_cache is True
    assert served.meta.as_of == CLOSED - timedelta(days=2), "as_of says exactly how old"


async def test_an_outage_with_nothing_cached_still_errors():
    """There is genuinely no answer to give — inventing one would be worse than failing."""
    service = FreezeService(calendar=ClosedMarket(), storage=InMemoryStorage())

    with pytest.raises(EdgeUnavailableError):
        await service.get("quote:NEVER-SEEN", dead_upstream)
