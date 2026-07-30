"""HTML parsing for PSE Edge pages (verified structure, 2026-07: th/td label rows)."""

from __future__ import annotations

import re
from datetime import date, datetime

from selectolax.parser import HTMLParser

from .errors import EndpointChangedError

_NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _to_float(text: str | None) -> float | None:
    if not text:
        return None
    m = _NUM_RE.search(text.replace("(", "-").replace(")", ""))
    return float(m.group().replace(",", "")) if m else None


def _to_int(text: str | None) -> int | None:
    f = _to_float(text)
    return int(f) if f is not None else None


def _to_date(text: str | None) -> date | None:
    if not text:
        return None
    m = re.search(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", text)
    if not m:
        return None
    return datetime.strptime(m.group(), "%b %d, %Y").date()


def parse_stock_data_page(html: str) -> dict:
    """Parse companyPage/stockData.do into a flat dict for StockQuote.

    The page renders quote + security profile as <tr><th>label</th><td>value</td></tr>
    rows. We harvest every pair (raw_fields) and map known labels to typed fields,
    so unknown/renamed labels degrade gracefully while known ones stay validated.
    """
    tree = HTMLParser(html)

    fields: dict[str, str] = {}
    for tr in tree.css("tr"):
        ths = tr.css("th")
        tds = tr.css("td")
        if ths and tds and len(ths) == len(tds):
            for th, td in zip(ths, tds, strict=True):
                label = th.text(strip=True)
                if label:
                    fields[label] = re.sub(r"\s+", " ", td.text(strip=True))

    if not fields:
        raise EndpointChangedError(
            "stockData.do: no th/td label rows found — page structure changed"
        )

    cmpy_input = tree.css_first("input[name=cmpy_id]")
    sec_select = tree.css_first("select[name=security_id]")
    company_id = cmpy_input.attributes.get("value", "") if cmpy_input else ""
    security_id = ""
    if sec_select:
        selected = sec_select.css_first("option[selected]") or sec_select.css_first("option")
        if selected:
            security_id = selected.attributes.get("value", "") or ""

    # Company name & symbol from the page header (e.g. "SM Investments Corporation [SM]")
    company_name, symbol = "", ""
    for node in tree.css("h2, h3, .compName, .compInfo"):
        text = node.text(strip=True)
        m = re.match(r"^(.*?)\s*\[?([A-Z0-9]{1,6})\]?$", text) if text else None
        if m and len(m.group(1)) > 3:
            company_name, symbol = m.group(1).strip(), m.group(2)
            break

    change_text = fields.get("Change(% Change)", "")
    change = _to_float(change_text)
    pct = None
    pct_m = re.search(r"\(([^)]*%)\)", change_text)
    if pct_m:
        pct = _to_float(pct_m.group(1))
    if change is not None and re.search(r"\bdown\b", change_text, re.I):
        change = -abs(change)
        pct = -abs(pct) if pct is not None else None

    return {
        "company_id": company_id,
        "security_id": security_id,
        "company_name": company_name,
        "symbol": symbol,
        "status": fields.get("Status"),
        "last_traded_price": _to_float(fields.get("Last Traded Price")),
        "open": _to_float(fields.get("Open")),
        "high": _to_float(fields.get("High")),
        "low": _to_float(fields.get("Low")),
        "previous_close": _to_float(fields.get("Previous Close and Date")),
        "change": change,
        "change_percent": pct,
        "volume": _to_int(fields.get("Volume")),
        "value": _to_float(fields.get("Value", fields.get("Value Traded", ""))),
        "week52_high": _to_float(fields.get("52-Week High")),
        "week52_low": _to_float(fields.get("52-Week Low")),
        "market_cap": _to_float(fields.get("Market Capitalization")),
        "outstanding_shares": _to_int(fields.get("Outstanding Shares")),
        "par_value": _to_float(fields.get("Par Value")),
        "isin": fields.get("ISIN"),
        "listing_date": _to_date(fields.get("Listing Date")),
        "free_float_percent": _to_float(fields.get("Free Float Level(%)")),
        "foreign_ownership_limit_percent": _to_float(fields.get("Foreign Ownership Limit(%)")),
        "raw_fields": fields,
    }
