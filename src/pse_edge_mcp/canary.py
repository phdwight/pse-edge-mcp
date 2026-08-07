"""Nightly schema canary (plan §6a): find out PSE Edge changed before a user does.

This server parses HTML that PSE Edge can restyle at any time, without warning and without
any obligation to us. Invariant #4 says drift must be loud — `EndpointChangedError` rather
than partial data — but loud *to whoever happens to call the affected tool next*. That
person is a user, and by then the failure is already theirs. The canary moves the discovery
earlier: it exercises one endpoint per family every night and tells the operator.

Three deliberate differences from a normal read:

- **It bypasses the cache.** `FreezeService` would happily answer from a warm entry, which
  would validate yesterday's HTML and prove nothing about today's. The canary calls the
  client directly, so every check is a real request and a real parse.
- **It still refuses to run while the market is open.** Bypassing the cache does not mean
  bypassing invariant #1. Protecting PSE Edge outranks knowing about drift promptly, and
  waiting until the close costs at most a few hours.
- **It validates the Pydantic model, not just the HTTP status.** A 200 with a restyled
  table is exactly the failure this exists to catch, and it is invisible at the HTTP layer.

One pass is ~8 requests once a day, which is the same order as a single user's session and
well inside the politeness throttle the client already applies.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .client import PseEdgeClient
from .config import Settings
from .market_calendar import MarketCalendar

logger = logging.getLogger(__name__)

# A liquid, long-listed issue that is unlikely to be suspended or delisted between runs.
# If the canary starts failing on every check at once, confirm this symbol still trades
# before assuming Edge changed — a delisting would look identical from here.
PROBE_SYMBOL = "SM"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: int = 0


@dataclass
class CanaryReport:
    checks: list[CheckResult] = field(default_factory=list)
    skipped: str | None = None

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        if self.skipped:
            return f"canary skipped: {self.skipped}"
        passed = len(self.checks) - len(self.failures)
        return f"{passed}/{len(self.checks)} endpoint families healthy"

    def as_text(self) -> str:
        lines = [self.summary(), ""]
        for check in self.checks:
            mark = "PASS" if check.ok else "FAIL"
            lines.append(f"[{mark}] {check.name} ({check.duration_ms} ms)")
            if not check.ok:
                lines.append(f"       {check.detail}")
        return "\n".join(lines)


async def run_canary(
    settings: Settings | None = None,
    *,
    calendar: MarketCalendar | None = None,
    client: Any | None = None,
) -> CanaryReport:
    """Exercise one endpoint per family and validate each against its model."""
    settings = settings or Settings.from_env()
    calendar = calendar or MarketCalendar(
        tz=settings.market_tz, open_time=settings.market_open, close_time=settings.market_close
    )
    report = CanaryReport()

    if calendar.is_market_open():
        # Invariant #1 outranks this check. Knowing about drift a few hours later is a
        # far smaller cost than being the reason PSE Edge sees traffic mid-session.
        report.skipped = "the market is open; upstream fetches are frozen (invariant #1)"
        logger.info("canary: %s", report.skipped)
        return report

    owned = client is None
    client = client or PseEdgeClient(settings)
    try:
        # Imported here so the module stays importable without the parse layer's cost, and
        # so a parser rename surfaces as a canary failure rather than an import error.
        from . import parsers
        from .models import (
            CompanyProfile,
            DividendRecord,
            FinancialPeriod,
            IndexQuote,
            OhlcBar,
            RightsRecord,
            StockQuote,
        )

        company_id: str | None = None
        security_id: str | None = None

        async def check(name: str, run: Any) -> Any:
            started = time.perf_counter()
            try:
                value = await run()
            except Exception as exc:  # noqa: BLE001 - every failure mode is a finding
                elapsed = int((time.perf_counter() - started) * 1000)
                report.checks.append(
                    CheckResult(name, False, f"{type(exc).__name__}: {exc}", elapsed)
                )
                return None
            report.checks.append(
                CheckResult(name, True, "", int((time.perf_counter() - started) * 1000))
            )
            return value

        async def companies() -> Any:
            nonlocal company_id
            hits = await client.search_companies(PROBE_SYMBOL)
            match = next(h for h in hits if h.get("symbol", "").upper() == PROBE_SYMBOL)
            company_id = match["cmpyId"]
            return match

        await check("search_companies (autocomplete JSON)", companies)
        if company_id is None:
            # Everything downstream needs a company id; without one the remaining checks
            # would report a cascade of failures that all have this one cause.
            report.checks.append(
                CheckResult(
                    "remaining checks",
                    False,
                    f"skipped: could not resolve {PROBE_SYMBOL} to a company id",
                )
            )
            return report

        async def quote() -> Any:
            nonlocal security_id
            html = await client.fetch_stock_data_page(company_id)
            parsed = parsers.parse_stock_data_page(html)
            security_id = parsed.get("security_id")
            StockQuote.model_validate(parsed)
            return parsed

        await check("get_stock_quote (stockData.do → StockQuote)", quote)

        if security_id:

            async def history() -> Any:
                end = date.today()
                data = await client.fetch_price_history(
                    company_id, security_id, end - timedelta(days=14), end
                )
                # Mirror QuoteRepository.history: the client guarantees `chartData` exists,
                # so what is left to verify is that each row still carries the six OHLC keys
                # under the names the model expects and that the date still parses.
                rows = data["chartData"]
                if not rows:
                    raise ValueError("chartData is empty for a two-week window")
                for row in rows:
                    OhlcBar(
                        trade_date=parsers.parse_chart_date(row["CHART_DATE"]),
                        open=row["OPEN"],
                        high=row["HIGH"],
                        low=row["LOW"],
                        close=row["CLOSE"],
                        value=row["VALUE"],
                    )
                return rows

            await check("get_price_history (DisclosureCht.ax JSON dialect)", history)

        async def disclosures() -> Any:
            end = date.today()
            html = await client.search_announcements(from_date=end - timedelta(days=7), to_date=end)
            return parsers.parse_disclosure_table(html)

        await check("search_disclosures (announcements/search.ax → HTML)", disclosures)

        async def profile() -> Any:
            html = await client.fetch_company_information(company_id)
            parsed = parsers.parse_company_profile(html)
            # Mirror CompanyInfoRepository.profile exactly, including the two fields it
            # injects: the profile page's own header is thinner than autocomplete's JSON.
            # A canary that validates a *different* construction than the tool would either
            # miss real drift or invent failures the tool never sees.
            parsed["company_id"] = company_id
            parsed["company_name"] = parsed.get("company_name") or PROBE_SYMBOL
            CompanyProfile(**parsed)
            return True

        await check("get_company_profile (companyInfo → CompanyProfile)", profile)

        async def financials() -> Any:
            html = await client.fetch_financial_reports(company_id)
            parsed = parsers.parse_financial_reports(html)
            # Validate the pieces the model is actually built from, the way the repository
            # builds it: a shape change shows up here as a validation error, not a KeyError
            # three layers away in a user's tool call.
            [FinancialPeriod(**period) for period in parsed["periods"]]
            return True

        await check("get_financial_highlights (financialReports → FinancialHighlights)", financials)

        async def dividends() -> Any:
            html = await client.fetch_dividends_or_rights(company_id, "Dividends")
            [DividendRecord(**row) for row in parsers.parse_dividends(html)]
            rights_html = await client.fetch_dividends_or_rights(company_id, "Rights")
            [RightsRecord(**row) for row in parsers.parse_rights(rights_html)]
            return True

        await check("get_dividends_and_rights (dividends_and_rights_list.ax)", dividends)

        async def indices() -> Any:
            html = await client.fetch_homepage()
            rows = [IndexQuote(**row) for row in parsers.parse_indices(html)]
            if not rows:
                raise ValueError("homepage returned no index rows")
            parsers.parse_market_summary(html)
            return True

        await check("get_indices (homepage → MarketIndices)", indices)
    finally:
        if owned:
            await client.aclose()

    if report.ok:
        logger.info("canary: %s", report.summary())
    else:
        # WARNING, not INFO: this is the signal that the parse layer no longer matches
        # what PSE Edge serves, and someone has to look at HTML.
        logger.warning(
            "canary: %s — failing families: %s",
            report.summary(),
            ", ".join(c.name for c in report.failures),
        )
    return report


async def run_and_notify(
    settings: Settings | None = None, *, sender: Any | None = None, to: str | None = None
) -> CanaryReport:
    """Run the canary and email the operator when something failed.

    Silent on success on purpose. A nightly "all fine" message is read for a week and
    filtered forever after, and a filtered alert is the same as no alert — worse, because
    it feels like coverage. The log records every run either way.
    """
    settings = settings or Settings.from_env()
    report = await run_canary(settings)

    if report.ok or report.skipped:
        return report

    to = to or settings.operator_email
    if not to:
        logger.warning(
            "canary: failures detected but PSE_OPERATOR_EMAIL is unset — nobody was told"
        )
        return report

    if sender is None:
        from .email import build_email_sender

        sender = build_email_sender(settings.zeptomail_api_key, settings.email_from)

    body = (
        "<p>The PSE Edge schema canary failed. The parse layer no longer matches what "
        "PSE Edge serves, so the affected tools are returning errors or partial data to "
        "users right now.</p><pre>" + report.as_text() + "</pre>"
        "<p>Re-capture the affected fixture, compare against docs/endpoints.md, and fix "
        "the parser. Do not relax the validation to make this pass.</p>"
    )
    try:
        await sender.send(to=to, subject=f"PSE Edge canary FAILED — {report.summary()}", html=body)
    except Exception:  # noqa: BLE001 - a broken mailer must not hide the finding
        logger.exception("canary: could not email the failure report; the log is the record")
    return report


def main() -> None:
    """`pse-edge-canary` — one pass, then exit non-zero if anything failed.

    Non-zero matters: it is what lets a cron wrapper, a compose healthcheck or a CI job
    notice without parsing output.
    """
    from .logging_config import configure_logging

    settings = Settings.from_env()
    configure_logging(json_output=settings.log_json, level=settings.log_level)
    report = asyncio.run(run_and_notify(settings))
    print(report.as_text())
    raise SystemExit(0 if report.ok or report.skipped else 1)


if __name__ == "__main__":
    main()
