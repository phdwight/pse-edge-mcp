"""HTML parsing for PSE Edge pages (verified structure, 2026-07: th/td label rows)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser

from .errors import EndpointChangedError

_NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")
_MANILA = ZoneInfo("Asia/Manila")


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


def _to_datetime(text: str | None) -> datetime | None:
    """Announce timestamps render as 'Jul 30, 2026 03:47 PM' (Asia/Manila)."""
    if not text:
        return None
    m = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?", text)
    if not m:
        return None
    if m.group(2):
        naive = datetime.strptime(
            f"{m.group(1)} {m.group(2).replace(' ', '')}", "%b %d, %Y %I:%M%p"
        )
    else:
        naive = datetime.strptime(m.group(1), "%b %d, %Y")
    return naive.replace(tzinfo=_MANILA)


def parse_chart_date(raw: str) -> date:
    """DisclosureCht.ax emits CHART_DATE as e.g. 'Jun 01, 2026 00:00:00'."""
    return datetime.strptime(raw, "%b %d, %Y %H:%M:%S").date()


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _in_document_order(tree: HTMLParser) -> list[Any]:
    """Every element node, in document order.

    Needed because neither alternative works: `Node.iter()` walks direct children only,
    and a comma CSS selector returns matches grouped by selector rather than in document
    order. Both silently mis-attribute content to the wrong section heading.
    """
    root = tree.root
    if root is None:
        return []
    return list(root.traverse(include_text=False))


def parse_stock_data_page(html: str) -> dict[str, Any]:
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


# --- disclosures ---------------------------------------------------------------
#
# announcements/search.ax and companyDisclosures/search.ax return the same kind of
# HTML table but with DIFFERENT column orders (announcements leads with Company Name;
# companyDisclosures omits it entirely). So we map cells by <thead> label, never by
# position — one parser serves both dialects and a reordered column is a non-event.

_EDGE_NO_RE = re.compile(r"openPopup\('([0-9a-f]{32})'\)")
_CMPY_ID_RE = re.compile(r"cmpy_id=(\d+)")
_FILE_ID_RE = re.compile(r"(downloadFile|downloadHtml)\.do\?file_id=(\d+)")
_PAGE_OF_RE = re.compile(r"\[\s*([\d,]+)\s*/\s*([\d,]+)\s*\]")
_TOTAL_RE = re.compile(r"\[\s*Total\s+([\d,]+)\s*\]")

_COLUMNS = {
    "company name": "company_name",
    "template name": "template",
    "pse form number": "pse_form_number",
    "announce date and time": "announced_at",
    "circular number": "circular_number",
    "report or circular number": "circular_number",
}

_ROW_FIELDS = (
    "edge_no",
    "template",
    "company_name",
    "company_id",
    "announced_at",
    "pse_form_number",
    "circular_number",
)

EDGE_NO_RE = re.compile(r"^[0-9a-f]{32}$")


def _to_count(text: str) -> int:
    return int(text.replace(",", ""))


def _parse_counts(tree: HTMLParser) -> dict[str, Any]:
    """The `<span class="count">` header carries `[page / pages] [Total n]`.

    docs/endpoints.md v2 claimed there is no total count and callers must paginate
    until a short page; the live capture shows the count span on every search.ax
    response, so pagination is exact. We still tolerate its absence.
    """
    node = tree.css_first("span.count")
    raw = _squash(node.text()) if node else ""
    page_m, total_m = _PAGE_OF_RE.search(raw), _TOTAL_RE.search(raw)
    return {
        "page": _to_count(page_m.group(1)) if page_m else None,
        "pages": _to_count(page_m.group(2)) if page_m else None,
        "total": _to_count(total_m.group(1)) if total_m else None,
    }


def parse_disclosure_table(html: str) -> dict[str, Any]:
    """Parse an announcements / companyDisclosures search.ax HTML fragment.

    Returns {"rows": [...], "page", "pages", "total", "unknown_columns"}. Rows are
    identified by their openPopup('<edge_no>') handler, which also skips the
    "no data." placeholder row that Edge emits for empty results.
    """
    tree = HTMLParser(html)
    table = tree.css_first("table.list") or tree.css_first("table")
    if table is None:
        raise EndpointChangedError("disclosure search: no result table in response")

    headers = [_squash(th.text()) for th in table.css("thead th")]
    if not headers:
        raise EndpointChangedError("disclosure search: result table has no header row")
    keys = [_COLUMNS.get(h.lower()) for h in headers]
    unknown = [h for h, k in zip(headers, keys, strict=True) if k is None]

    rows: list[dict[str, Any]] = []
    for tr in table.css("tbody tr"):
        edge_m = _EDGE_NO_RE.search(tr.html or "")
        if edge_m is None:
            continue  # placeholder ("no data.") row
        cells = tr.css("td")
        if len(cells) != len(keys):
            raise EndpointChangedError(
                f"disclosure search: row has {len(cells)} cells but header declares "
                f"{len(keys)} — column layout changed"
            )
        # Seed every field: the two dialects expose different column sets (only
        # announcements carries Company Name), and callers should not have to care.
        row: dict[str, Any] = {field: None for field in _ROW_FIELDS}
        row["edge_no"] = edge_m.group(1)
        for key, td in zip(keys, cells, strict=True):
            if key is None:
                continue
            text = _squash(td.text())
            if key == "announced_at":
                row[key] = _to_datetime(text)
            elif key == "company_name":
                row[key] = text or None
                cmpy_m = _CMPY_ID_RE.search(td.html or "")
                row["company_id"] = cmpy_m.group(1) if cmpy_m else None
            else:
                row[key] = text or None
        rows.append(row)

    counts = _parse_counts(tree)
    if counts["total"] and not rows:
        raise EndpointChangedError(
            f"disclosure search: response claims {counts['total']} results but no rows "
            "could be parsed — row markup changed"
        )
    return {"rows": rows, "unknown_columns": unknown, **counts}


def parse_keyword_results(html: str) -> dict[str, Any]:
    """Parse keyword/search.ax — a <dl> of full-text hits, not a table.

    Each hit is a <dt> (subject + openPopup edge_no) followed by <dd>s whose roles we
    detect by content (attachment link / company link / timestamp / snippet) rather
    than by position, since the set of <dd>s varies per hit.
    """
    tree = HTMLParser(html)
    counts = _parse_counts(tree)
    dl = tree.css_first("dl")
    if dl is None:
        raise EndpointChangedError("keyword search: no <dl> result list in response")

    hits: list[dict[str, Any]] = []
    for node in dl.iter():
        tag, text = node.tag, _squash(node.text())
        if tag == "dt":
            edge_m = _EDGE_NO_RE.search(node.html or "")
            if edge_m is None:
                continue  # "no data." placeholder
            hits.append(
                {
                    "edge_no": edge_m.group(1),
                    "subject": text or None,
                    "company_name": None,
                    "company_id": None,
                    "circular_number": None,
                    "announced_at": None,
                    "attachment_file_id": None,
                    "attachment_filename": None,
                    "snippet": None,
                }
            )
            continue
        if tag != "dd" or not hits:
            continue

        hit, inner = hits[-1], node.html or ""
        if (file_m := _FILE_ID_RE.search(inner)) is not None:
            hit["attachment_file_id"] = file_m.group(2)
            hit["attachment_filename"] = text or None
        elif (cmpy_m := _CMPY_ID_RE.search(inner)) is not None:
            hit["company_id"] = cmpy_m.group(1)
            hit["company_name"] = text or None
        elif (announced := _to_datetime(text)) is not None and len(text) < 40:
            hit["announced_at"] = announced
        elif re.fullmatch(r"[A-Z]{1,3}\d{3,6}-\d{4}", text):
            hit["circular_number"] = text
        elif text:
            hit["snippet"] = text

    if counts["total"] and not hits:
        raise EndpointChangedError(
            f"keyword search: response claims {counts['total']} results but no hits "
            "could be parsed — result markup changed"
        )
    return {"hits": hits, **counts}


def parse_disclosure_viewer(html: str) -> dict[str, Any]:
    """Parse openDiscViewer.do into disclosure metadata + attachment links.

    Per plan §2 we expose links only — no attachment download or PDF parsing. The
    rendered disclosure body lives behind downloadHtml.do?file_id=<body_file_id>,
    which we surface as a URL for the MCP client to fetch itself if it wants it.
    """
    tree = HTMLParser(html)

    header = tree.css_first("#viewHeader")
    if header is None:
        raise EndpointChangedError("openDiscViewer.do: no #viewHeader — page structure changed")
    name_node = header.css_first("h2")
    date_node = header.css_first("p")
    title_node = tree.css_first("title")

    documents: list[dict[str, Any]] = []
    current_edge_no = None
    for opt in tree.css("#docList option"):
        value = opt.attributes.get("value") or ""
        if not EDGE_NO_RE.match(value):
            continue
        is_current = "selected" in opt.attributes
        if is_current:
            current_edge_no = value
        documents.append({"edge_no": value, "label": _squash(opt.text()), "is_current": is_current})

    attachments: list[dict[str, Any]] = []
    for opt in tree.css("#file_list option"):
        file_id = opt.attributes.get("value") or ""
        if not file_id.isdigit():
            continue  # the "Select" prompt option
        attachments.append(
            {
                "file_id": file_id,
                "filename": _squash(opt.text()),
                "download_url": f"/downloadFile.do?file_id={file_id}",
            }
        )

    body_file_id = None
    iframe = tree.css_first("#viewContents")
    if iframe is not None:
        body_m = _FILE_ID_RE.search(iframe.attributes.get("src") or "")
        if body_m:
            body_file_id = body_m.group(2)

    if not documents and body_file_id is None:
        raise EndpointChangedError(
            "openDiscViewer.do: neither a document list nor a body iframe found — "
            "page structure changed"
        )

    return {
        "edge_no": current_edge_no or (documents[0]["edge_no"] if documents else None),
        "template": _squash(title_node.text()) if title_node else None,
        "company_name": _squash(name_node.text()) if name_node else None,
        "disclosure_date": _to_date(_squash(date_node.text()) if date_node else None),
        "documents": documents,
        "attachments": attachments,
        "body_file_id": body_file_id,
        "body_html_url": f"/downloadHtml.do?file_id={body_file_id}" if body_file_id else None,
    }


# --- company info & market ---------------------------------------------------

_PROFILE_FIELDS = {
    "sector": "sector",
    "subsector": "subsector",
    "corporate life": "corporate_life",
    "incorporation date": "incorporation_date",
    "number of directors": "number_of_directors",
    "stockholders' meeting as per by-laws": "stockholders_meeting",
    "fiscal year": "fiscal_year",
    "external auditor": "external_auditor",
    "transfer agent": "transfer_agent",
    "business address": "business_address",
    "e-mail address": "email",
    "telephone number": "telephone",
    "fax number": "fax",
    "website": "website",
}

_DATE_PROFILE_FIELDS = {"incorporation_date"}
_INT_PROFILE_FIELDS = {"number_of_directors"}


def _label_pairs(tree: HTMLParser) -> dict[str, str]:
    """Harvest every <tr><th>label</th><td>value</td></tr> pair on a page."""
    fields: dict[str, str] = {}
    for tr in tree.css("tr"):
        ths, tds = tr.css("th"), tr.css("td")
        if ths and tds and len(ths) == len(tds):
            for th, td in zip(ths, tds, strict=True):
                label = _squash(th.text())
                if label:
                    fields[label] = _squash(td.text())
    return fields


def _company_name(tree: HTMLParser) -> str | None:
    """Company pages print the name in `div.compInfo > p`."""
    node = tree.css_first(".compInfo p") or tree.css_first(".compInfo")
    return _squash(node.text()) if node else None


def parse_company_profile(html: str) -> dict[str, Any]:
    """Parse companyInformation/form.do into CompanyProfile fields."""
    tree = HTMLParser(html)
    fields = _label_pairs(tree)
    if not fields:
        raise EndpointChangedError(
            "companyInformation/form.do: no th/td label rows found — page structure changed"
        )

    parsed: dict[str, Any] = {"company_name": _company_name(tree) or "", "raw_fields": fields}
    for label, value in fields.items():
        key = _PROFILE_FIELDS.get(label.lower())
        if key is None:
            continue
        if key in _DATE_PROFILE_FIELDS:
            parsed[key] = _to_date(value)
        elif key in _INT_PROFILE_FIELDS:
            parsed[key] = _to_int(value)
        else:
            parsed[key] = value or None
    return parsed


def _statement_table(node: Any) -> dict[str, Any]:
    """Parse one `table.view` statement: header row then <th>item</th><td>...</td> rows."""
    caption = node.css_first("caption")
    rows = node.css("tr")
    columns: list[str] = []
    items: dict[str, list[float | None]] = {}

    for tr in rows:
        ths, tds = tr.css("th"), tr.css("td")
        if ths and not tds:  # header row: Item | Current Year | Previous Year
            labels = [_squash(th.text()) for th in ths]
            columns = labels[1:]  # drop the "Item" column label
            continue
        if ths and tds:
            label = _squash(ths[0].text())
            if label:
                items[label] = [_to_float(_squash(td.text())) for td in tds]
    return {
        "statement": _squash(caption.text()) if caption else "",
        "columns": columns,
        "items": items,
    }


def parse_financial_reports(html: str) -> dict[str, Any]:
    """Parse companyPage/financial_reports_view.do into annual + quarterly sections.

    Layout: an `<h3>Annual</h3>` / `<h3>Quarterly</h3>` heading, then a `p.textCont`
    carrying the period and the currency/units label, then one `table.view` per
    statement. Sections are walked in document order so each statement is attributed to
    the heading above it.

    Values are returned exactly as printed. Each section's units label is captured
    verbatim because the two sections disagree in practice (annual "Php (in thousands)"
    vs quarterly "Php (in Millions)" for the same company), so rescaling here would
    encode Edge's inconsistency as fact.
    """
    tree = HTMLParser(html)

    periods: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    # Document order is what makes the heading -> period -> statements attribution work,
    # and only `traverse` provides it. Two traps ruled out by testing: `iter()` walks
    # direct children only, so it sees none of these (they are nested), and a comma CSS
    # selector returns matches GROUPED BY SELECTOR, not in document order — every h3
    # first, then every p — which silently filed all four statements under the last
    # heading.
    for node in _in_document_order(tree):
        tag = node.tag
        if tag == "h3":
            label = _squash(node.text()).lower()
            if label in ("annual", "quarterly"):
                current = {
                    "period_type": label,
                    "period_label": None,
                    "period_ended": None,
                    "currency_units": None,
                    "statements": [],
                }
                periods.append(current)
            continue
        if current is None:
            continue
        if tag == "p" and "textCont" in (node.attributes.get("class") or ""):
            text = _squash(node.text())
            if "ended" in text.lower() and current["period_label"] is None:
                current["period_label"] = text.split("Currency")[0].strip()
                current["period_ended"] = _to_date(current["period_label"])
            m = re.search(r"Currency\(and units[^)]*\)\s*:\s*(.+)$", text)
            if m:
                current["currency_units"] = m.group(1).strip()
            continue
        if tag == "table" and "view" in (node.attributes.get("class") or ""):
            current["statements"].append(_statement_table(node))

    note_node = tree.css_first("p.textCont")
    note = _squash(note_node.text()) if note_node else None
    if note and "ended" in note.lower():
        note = None  # that was a period line, not a page-level notice

    if not periods and note is None:
        raise EndpointChangedError(
            "financial_reports_view.do: no Annual/Quarterly sections and no notice — "
            "page structure changed"
        )
    return {"periods": periods, "note": note}


_DIVIDEND_COLUMNS = {
    "type of security": "security_type",
    "type of dividend": "dividend_type",
    "dividend rate": "dividend_rate",
    "ex-dividend date": "ex_dividend_date",
    "record date": "record_date",
    "payment date": "payment_date",
    "circular number": "circular_number",
}

_RIGHTS_COLUMNS = {
    "entitlement ratio": "entitlement_ratio",
    "offer price": "offer_price",
    "ex-rights date": "ex_rights_date",
    "start of offer period": "offer_start",
    "end of offer period": "offer_end",
    "circular number": "circular_number",
}

_DIVIDEND_DATE_FIELDS = {"ex_dividend_date", "record_date", "payment_date"}
_RIGHTS_DATE_FIELDS = {"ex_rights_date", "offer_start", "offer_end"}


def _parse_record_table(
    html: str, columns: dict[str, str], date_fields: set[str], what: str
) -> list[dict[str, Any]]:
    """Header-driven parse of a dividends/rights `table.list` fragment.

    Same principle as the disclosure tables: map cells by `<thead>` label, never by
    position. The circular-number cell carries an `openPopup('<edge_no>')` handler, so
    each record links back to the disclosure that announced it.
    """
    tree = HTMLParser(html)
    table = tree.css_first("table.list")
    if table is None:
        raise EndpointChangedError(f"{what}: no table.list in response")
    headers = [_squash(th.text()) for th in table.css("thead th")]
    if not headers:
        raise EndpointChangedError(f"{what}: result table has no header row")
    keys = [columns.get(h.lower()) for h in headers]

    records: list[dict[str, Any]] = []
    for tr in table.css("tbody tr"):
        cells = tr.css("td")
        if len(cells) != len(keys):
            continue  # the "no data." placeholder spans all columns
        record: dict[str, Any] = {field: None for field in set(columns.values()) | {"edge_no"}}
        for key, td in zip(keys, cells, strict=True):
            if key is None:
                continue
            text = _squash(td.text())
            if key in date_fields:
                record[key] = _to_date(text)
            else:
                record[key] = text or None
            if key == "circular_number":
                edge = _EDGE_NO_RE.search(td.html or "")
                record["edge_no"] = edge.group(1) if edge else None
        records.append(record)
    return records


def parse_dividends(html: str) -> list[dict[str, Any]]:
    return _parse_record_table(html, _DIVIDEND_COLUMNS, _DIVIDEND_DATE_FIELDS, "dividends list")


def parse_rights(html: str) -> list[dict[str, Any]]:
    return _parse_record_table(html, _RIGHTS_COLUMNS, _RIGHTS_DATE_FIELDS, "rights list")


_ARROW_UP, _ARROW_DOWN = "▲", "▼"
_CIRCULAR_RE = re.compile(r"^[A-Z]{1,3}\d{3,6}-\d{4}$")


def _index_rows(table: Any) -> list[dict[str, Any]]:
    """Rows of the Index Summary table.

    Edge prints `Chg` and `%Chg` **unsigned** and encodes direction only in a CSS colour
    and a ▲/▼ glyph, so a naive parse reports every decline as a gain. The glyph is the
    signal we trust (colour is presentation and could be restyled); the sign is applied
    from it.
    """
    rows: list[dict[str, Any]] = []
    for tr in table.css("tr"):
        label = tr.css_first("td.label")
        cells = tr.css("td")
        if label is None or len(cells) < 4:
            continue
        name = _squash(label.text())
        if not name:
            continue
        raw_change, raw_pct = _squash(cells[2].text()), _squash(cells[3].text())
        direction = (
            "down"
            if _ARROW_DOWN in raw_change + raw_pct
            else "up"
            if _ARROW_UP in raw_change + raw_pct
            else "flat"
        )
        sign = -1.0 if direction == "down" else 1.0
        change, pct = _to_float(raw_change), _to_float(raw_pct)
        rows.append(
            {
                "name": name,
                "value": _to_float(_squash(cells[1].text())),
                "change": None if change is None else sign * abs(change),
                "change_percent": None if pct is None else sign * abs(pct),
                "direction": direction,
            }
        )
    return rows


def parse_indices(html: str) -> list[dict[str, Any]]:
    """Parse the Index Summary block off the PSE Edge homepage.

    There is no AJAX feed for indices — the homepage renders them server-side, so this
    parses the page itself (verified at endpoint capture).
    """
    tree = HTMLParser(html)
    container = tree.css_first("div.index")
    table = container.css_first("table") if container else None
    if table is None:
        raise EndpointChangedError(
            "homepage: no div.index table — the Index Summary block moved or changed"
        )
    rows = _index_rows(table)
    if not rows:
        raise EndpointChangedError(
            "homepage: Index Summary table found but no index rows parsed — row markup changed"
        )
    return rows


def _feed_item(li: Any) -> dict[str, Any] | None:
    """One homepage feed entry.

    Two shapes exist (the disclosure feeds lead with the title, most-viewed leads with the
    symbol), so fields are identified by content — the openPopup handler, a cmpy_id link,
    a timestamp, a circular number — rather than by position.
    """
    inner = li.html or ""
    edge = _EDGE_NO_RE.search(inner)
    if edge is None:
        return None

    title, symbol = None, None
    for anchor in li.css("a"):
        text = _squash(anchor.text())
        if "openPopup" in (anchor.attributes.get("onclick") or ""):
            title = text
        elif "cmpy_id=" in (anchor.attributes.get("href") or ""):
            symbol = text
    cmpy = _CMPY_ID_RE.search(inner)

    announced_at, circular = None, None
    for span in li.css("span"):
        text = _squash(span.text())
        if not text or text == symbol:
            continue
        if announced_at is None and (parsed := _to_datetime(text)) is not None:
            announced_at = parsed
        elif _CIRCULAR_RE.match(text):
            circular = text

    return {
        "title": title or _squash(li.text()),
        "symbol": symbol or None,
        "company_id": cmpy.group(1) if cmpy else None,
        "announced_at": announced_at,
        "circular_number": circular,
        "edge_no": edge.group(1),
    }


def parse_market_summary(html: str) -> dict[str, Any]:
    """Parse the homepage into indices plus its labelled feeds.

    Feeds are keyed by Edge's own group labels (`Company Announcements`,
    `Financial Reports`, `Other Reports`, `Listing Notices`, `Disclosure Notices`,
    `Today`, `This Week`) rather than being folded into invented buckets, so what a caller
    sees matches what the site says. A relabelled or new group shows up on its own instead
    of being silently dropped.

    Walked with `traverse` for document order — a label (`dt`/`h3`) applies to the `ul`
    that follows it. See parse_financial_reports for why `css()` cannot be used here.
    """
    tree = HTMLParser(html)
    feeds: dict[str, list[dict[str, Any]]] = {}
    label: str | None = None

    for node in _in_document_order(tree):
        if node.tag in ("dt", "h3"):
            text = _squash(node.text())
            label = text or None
        elif node.tag == "ul" and label:
            items = [item for li in node.css("li") if (item := _feed_item(li)) is not None]
            if items:
                feeds.setdefault(label, []).extend(items)

    indices = parse_indices(html)
    if not feeds:
        raise EndpointChangedError(
            "homepage: indices parsed but no disclosure feeds found — page structure changed"
        )
    return {"indices": indices, "feeds": feeds}
