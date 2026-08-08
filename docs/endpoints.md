# PSE Edge Endpoint Recon — v4 (Phase 3 fixture pass)

**Date:** 2026-07-30 (v2 same day; v3 the disclosure pass; v4 the company-info & market pass)
**Base URL:** `https://edge.pse.com.ph`
**Method:** live probes from cloud (US IP), community source-mining, **live browser network capture + request replay** (Chrome, Manila IP), and **curl param sweeps during closed hours** (v3).

**Legend:** ✅ VERIFIED (live, request+response confirmed) · 📖 COMMUNITY (from working OSS code, not re-verified) · ⚠️ EXISTS (endpoint confirmed live, params need fixture work)

## v3 corrections to v2 (read these — v2 was wrong on all three)

1. **There IS a total count.** v2 said "no total-count text — detect last page by short
   row count". Every `search.ax` response carries `<span class="count">[page / pages]
   [Total n]</span>`, so pagination is exact and blind iteration is unnecessary.
2. **`search_disclosures` maps to `/announcements/search.ax`, not `/keyword/search.ax`.**
   v2 got this backwards. `keyword/search.ax` is a full-text search over *attachment
   text* whose index is partial and stale (see §3), so it cannot serve "recent
   disclosures". `announcements/search.ax` is the market-wide, date-ranged list.
3. **Page size differs per endpoint:** 50 rows for `announcements` /
   `companyDisclosures`, but **10** for `keyword`.

---

## 0. Protocol rules (verified — these are the big Phase 0 wins)

1. **Two request styles for `.ax` endpoints:**
   - **JSON-RPC style** (e.g. `DisclosureCht.ax`): `POST` with `Content-Type: application/json` and a JSON body. Sending form-urlencoded returns **HTTP 415**. Responses are JSON.
   - **Form style** (all `search.ax` endpoints): `POST` with `Content-Type: application/x-www-form-urlencoded`. Responses are **HTML fragments**.
2. **No cookies or session required** — every endpoint tested worked with credentials omitted, from both PH and US IPs. No `X-Requested-With` needed either.
3. **Watch for renamed params:** page form fields don't always match wire params (form has `startDt`/`endDt`, the wire wants `startDate`/`endDate`). Trust captures, not HTML.
4. **Dates in requests:** `MM-dd-yyyy`. Dates in responses vary (e.g. `"Jun 01, 2026 00:00:00"`, `"Aug 12, 2025 09:15 PM"`). Normalize to ISO-8601 Asia/Manila.

## 1. Company search & directory

### ✅ `GET /autoComplete/searchCompanyNameSymbol.ax?term={q}`
Clean JSON: `[{cmpyId, cmpyNm, symbol, etfYn}]`. → `search_companies`.

### ✅ `POST /cm/companySearch.ax` (form style)
Company-search popup backend (found via `goPagePop`). Params likely `pNum`/`companySearch`/`keyword` — fixture pass in Phase 1.

### 📖 `POST /companyDirectory/search.ax` (form style)
Paginated full directory. Params: `pageNo`, `companyId`, `keyword`, `sortType`, `dateSortType=DESC`, `cmpySortType=ASC`, `symbolSortType=ASC`, `sector=ALL`, `subsector=ALL`. HTML rows carry `cmpy_id` + `security_id` in onclick — **the source for security_id**. Sector taxonomy (from advanced-search dropdowns): 8 sectors + ETF; 24 subsectors.

## 2. Quotes & prices

### ✅ `GET /companyPage/stockData.do?cmpy_id={id}`
**There is no quote JSON endpoint** — the browser capture confirms the quote header block (last price, 52-wk range, market cap, etc.) is server-rendered in this HTML. `get_stock_quote` = parse this page. Page also contains hidden `cmpy_id` + selected `security_id` — a cheap per-symbol way to resolve security_id without walking the directory.

### ✅ `POST /common/DisclosureCht.ax` (JSON style) — **fully verified end-to-end**
Request body: `{"cmpy_id": "599", "security_id": "520", "startDate": "06-01-2026", "endDate": "07-30-2026"}`
Response:

```
{
  "chartData": [ {"OPEN": n, "HIGH": n, "LOW": n, "CLOSE": n, "VALUE": n, "CHART_DATE": "Jun 01, 2026 00:00:00"}, ... ],
  "tableData": [ {"SM_DM": ..., "SUBJECT_TITLE": ...}, ... ]   // disclosure markers overlaid on the chart
}
```

Default page load returns ~250 rows (≈1 trading year); arbitrary ranges honored (44 rows for Jun–Jul 2026). → `get_price_history`.

## 3. Disclosures

### ✅ `POST /announcements/search.ax` (form style) — **the primary disclosure search**
Params: `pageNo`, `keyword`, `tmplNm` (template filter, free text), `companyId`,
`fromDate`, `toDate` (**`MM-dd-yyyy`**), `sortType`, `dateSortType=DESC`, `cmpySortType`.
Date range is required in practice (v2's empty-param replay returning 0 rows is why it
looked broken). Verified: Jul 1–30 2026 → `[Total 804]`, 17 pages; single day → 36 rows;
`companyId=599` + Jan–Jul 2026 → 105.

Columns: **Company Name | Template Name | PSE Form Number | Announce Date and Time |
Circular Number**. 50 rows/page. Company cell links `/companyInformation/form.do?cmpy_id=N`
(the market-wide company id source). Each template cell: `openPopup('<32-hex edge_no>')`.
Fixtures: `announcements_search.html`, `announcements_short_page.html`, `announcements_empty.html`.

### ✅ `POST /companyDisclosures/search.ax` (form style) — per-company full history
Params: `pageNo`, `keyword`, `tmplNm`, **`sortType=date`**, `dateSortType=DESC`, `cmpySortType`.
**`keyword` must be the numeric company id** — a ticker symbol returns `[Total 0]`
(verified: `keyword=SM` → 0 rows; `keyword=599` → `[Total 343]`, 7 pages). No date
filter, so this is the endpoint for complete history; use `announcements` for windows.

**⚠️ `sortType` must be the literal `"date"` — `dateSortType` alone does nothing here.**
Found by manual testing on 2026-07-30, after v0.2.0 shipped with `sortType=""`. Measured
on `keyword=599`, page 1:

| `sortType` | `dateSortType` | Page 1 leads with |
|---|---|---|
| `""` | `DESC` | Aug 12 2024, Aug 07 2024, Oct 16 2024, May 30 2025, May 26 2026 — **no order at all** |
| `date` | `DESC` | Jul 29 2026, Jul 27 2026, Jul 23 2026 — newest first ✅ |
| `date` | `ASC` | Aug 06 2024, Aug 07 2024, Aug 07 2024 — oldest first ✅ |

The value comes from the page's own sort control, which posts
`goSort('/companyDisclosures/search.ax','date','DESC')` — the anchor was in the v2
capture, but its `sortType` argument was not carried into the request. Consequence while
it was wrong: "what did this company disclose recently" returned two-year-old filings on
page 1, and because rows were unordered, no amount of paging fixed it.

**Contrast: `announcements/search.ax` needs no `sortType`.** Verified both ways on the
same window — `sortType=""` and `sortType="date"` return byte-identical ordering
(`[Total 806]`, newest first). It defaults to date DESC; this endpoint does not. Don't
assume a shared default across `search.ax` endpoints.

Columns: **Template Name | Announce Date and Time | PSE Form Number | Report or Circular
Number** — a *different order and set* from `announcements` (no Company Name), and the
circular header is worded differently. Parse cells by `<thead>` label, never by position.
Fixtures: `company_disclosures_search.html`, `company_disclosures_last_page.html` (43 rows).

### ✅ `POST /keyword/search.ax` (form style) — full-text search inside attachments
From `/keyword/form.do`. Params: `keyword`, `fromDate`, `toDate` (`MM-dd-yyyy`),
`companyId`, `cmpyNm`, `subjectTitle`, `sector`, `subsector`, `pageNo`.

**Not a disclosure list** — it searches the *text of disclosure attachments* and returns
relevance-ordered hits with highlighted snippets, **10 per page**, as a `<dl>`: `dt`
(subject + `openPopup(edge_no)`), then `dd`s for circular no, attachment
(`/downloadFile.do?file_id=N` + filename), company (`cmpy_id=N`), announce datetime, and
the matched snippet with `<em class="emorange embold">` around hits.

**⚠️ The index is partial and stale.** Measured coverage for `keyword=dividend`:
`[Total 10,666]` overall; by year — 2022: 0, 2023: 4,955, 2024: 5,380, 2025: 331,
2026: **0** (and 4,955+5,380+331 = 10,666 exactly, so those three years *are* the whole
index). Anything filed in 2026 is unfindable here. Surface this limit to users rather
than reporting "no disclosures".

Param traps: `startDate`/`startDt` are silently **ignored** (they return the unfiltered
total, which looks like success); only `fromDate`/`toDate` filter, and only in
`MM-dd-yyyy` — ISO or slashed dates yield `[Total 0]`. Fixture: `keyword_search.html`.

### ✅ `GET /openDiscViewer.do?edge_no={32-hex}` — **fully verified end-to-end**
Small page (~4.9 KB) holding metadata + ids; the disclosure content itself is in an iframe.
Structure (fixture: `disclosure_viewer.html`):
- `<title>` = template name; `#viewHeader h2` = company; `#viewHeader p` = `Disclosure Date : Jul 30, 2026`
- `select#docList` — related documents, `option value` = each `edge_no`, `selected` marks
  the current one (amendment chains / multi-part filings appear here)
- `select#file_list` — attachments, `option value` = `file_id`; the first option is a
  `value=""` "Select" prompt and must be skipped. Download button posts `file_id` to
  `/downloadFile.do`.
- `iframe#viewContents src="/downloadHtml.do?file_id={int}"` — the rendered body

**The body `file_id` differs from the attachment `file_id`** (verified: body 1949127,
attachment 1949133) — one disclosure has several. `edge_no` is the stable natural key and
a published disclosure never changes, so details are cached permanently

Since 0.16.0 the server also fetches `GET /downloadFile.do?file_id={int}` itself, for the
`pse-edge://attachment/{file_id}` MCP resource: plain GET, raw file bytes back (usually
`application/pdf`; observed ~KBs to a few MB — a live SM Form 23-B was 2.7 MB), fetched
once ever per file (immutable cache), refused above 10 MB.
(`data_policy: "immutable"`, no boundary refetch).

`downloadHtml.do` returns a standalone XHTML doc with content in `#contentBox` (built by a
Korean vendor — the source carries Korean comments about XHTML doctypes for PDF export).
Parsing it is deliberately **out of v1 scope** (plan §2: metadata + links only); the tools
return its URL so the MCP client can fetch it.

## 4. Financial reports & company info

### ✅ `GET /companyInformation/form.do?cmpy_id={id}` — **verified** → `get_company_profile`
14 `<tr><th>label</th><td>value</td></tr>` pairs, same shape as `stockData.do`: Sector,
Subsector, Corporate Life, Incorporation Date, Number of Directors, Stockholders' Meeting
as per By-Laws, Fiscal Year, External Auditor, Transfer Agent, Business Address, E-mail
Address, Telephone Number, Fax Number, Website. Company name is in `div.compInfo > p`
(not an `h2`). Fixture: `company_profile.html`.

### ✅ `GET /companyPage/financial_reports_view.do?cmpy_id={id}` — **verified**
Layout: `<h3>Annual</h3>` / `<h3>Quarterly</h3>`, each followed by a `p.textCont` holding
the period (`For the fiscal year ended : Dec 31, 2025`) and a units label, then one
`table.view` per statement (`<caption>Balance Sheet</caption>`, `Income Statement`). Rows
are `<th>item</th><td>…</td>`; negatives render `(1,234)`.

**⚠️ Units differ between the two sections and Edge's labels are not trustworthy.**
Measured for SM on 2026-07-30: the annual section said `Php (in thousands)`, the quarterly
said `Php (in Millions)` — and the *same* figure (Total Assets 1,811,801) appears in both,
as the annual current year and as the quarterly audited comparative. It cannot be both.
For reference SM's real total assets are ≈₱1.8 **trillion**, consistent with millions, so
the annual "thousands" label looks simply wrong. **Never rescale these values.** Capture
`currency_units` verbatim per period and let the caller judge.

Column labels also differ per section: annual is `Current Year` / `Previous Year`;
quarterly balance sheet is `Period Ended` / `Fiscal Year Ended(Audited)`; the quarterly
income statement is **four** columns (`Current Year (3 Months)`, `Previous Year (3
Months)`, `Current Year-To-Date`, `Previous Year-To-Date`). Companies that have not filed
show only a notice and no tables — degrade to an empty result, don't raise.
Fixture: `financial_reports.html`.

### ✅ `POST /companyPage/dividends_and_rights_list.ax?DividendsOrRights={Dividends|Rights}`
**`dividends_and_rights_form.do` is only a shell — it contains no data** (0 tables). It
posts to this `.ax` per tab via `goTab()`. Note the split: `DividendsOrRights` travels in
the **query string**, `cmpy_id` in the **form body**. Two calls are needed for the full
picture.

Both responses are header-driven `table.list` fragments, so the disclosure-table approach
applies. Dividends columns: Type of Security, Type of Dividend, Dividend Rate,
Ex-Dividend Date, Record Date, Payment Date, Circular Number. Rights columns: Entitlement
Ratio, Offer Price, Ex-Rights Date, Start of Offer Period, End of Offer Period, Circular
Number. The circular cell carries `openPopup('<edge_no>')`, so each record links back to
the disclosure that announced it. Rates are printed as text (`Php17.00`) and are not all
amounts — keep verbatim. Empty tabs give the usual `no data.` placeholder.
Fixtures: `dividends.html`, `rights_empty.html`.

### ⚠️ `GET /companyPage/directors_and_management_list.do?cmpy_id={id}` (not yet a tool)
Two `table.list` blocks, `<caption>Board of Directors</caption>` and
`<caption>Management Officers</caption>`, columns Position | Name. Structure confirmed;
no tool exposes it yet (not in the plan's v1 surface). Cheap to add when wanted.

## 5. Indices & market-wide

### ✅ `GET /` — indices are **server-rendered HTML** (no AJAX feed exists) — **verified**
The Index Summary lives in `div.index > table`: `thead` Index | Value | Chg | %Chg, rows
`<td class="label">PSEi</td>` plus three `td.alignR`. All 8 present: PSEi, All Shares,
Financials, Industrial, Holding Firms, Property, Services, Mining and Oil.

**⚠️ `Chg` and `%Chg` are printed UNSIGNED.** Direction is encoded only in
`style="color:red|green"` and a trailing ▼/▲ glyph, so a naive parse reports every decline
as a gain — on the capture, PSEi printed `47.29` while actually **falling** 47.29 (red, ▼).
Derive the sign from the glyph (colour is presentation and could be restyled).

Homepage feeds are grouped under `dl > dt` labels — `Company Announcements`,
`Financial Reports`, `Other Reports` (Company Disclosures section), `Listing Notices`,
`Disclosure Notices` (Exchange Notices section) — plus `h3 Today` / `h3 This Week` for the
5 Most Viewed block, and a Halts and Suspensions block. Feed `li` markup comes in **two
shapes**: the disclosure feeds lead with the title then spans for symbol/timestamp/circular,
while most-viewed leads with the symbol link. Identify fields by content, not position.
Fixture: `homepage.html`.

**Confirmed: no gainers/losers/most-active section exists on Edge.** Dropped from v1 scope (PSE's main site, not Edge, would be the source if ever wanted).

### ⚠️ Parsing trap for these multi-section pages (bit us twice)
Attributing content to the heading above it needs true document order, and selectolax
offers three walks that are *not* equivalent:
- `Node.iter()` — direct children only. These nodes are nested, so it yields nothing.
- `tree.css("h3, p, table")` — returns matches **grouped by selector** (every `h3`, then
  every `p`, then every `table`), NOT document order. This silently filed all four
  financial statements under the last heading and left the annual section empty.
- `tree.root.traverse(include_text=False)` — genuine document order. **Use this**; it is
  wrapped as `_in_document_order()` in `parsers.py`.

### Market-wide pages (existence ✅, parse TBD): `/index/form.do`, `/psei/form.do`, `/disclosureData/dividends_and_rights_info_form.do`, `/disclosureData/halts_and_suspensions_list.do`, `/disclosureData/listing_applicants_list.do`, `/disclosureData/etf_form.do`, `/disclosureNotices/form.do`, `/listingNotices/form.do`, `/otherReports/form.do`, `/companyPage/marketCalendar.do`

## 6. Implications for the build

1. **`get_stock_quote` is an HTML parse**, not JSON — slightly more Phase 1 parser work; recorded HTML fixtures are essential.
2. **`security_id` resolution:** parse it straight off `stockData.do` per company (simpler than directory pagination); directory sync still populates the Postgres `companies` table.
3. **`search_disclosures` maps to `/announcements/search.ax`** (market-wide + date range,
   optional `companyId`), with `companyDisclosures/search.ax` for a company's full
   history and `keyword/search.ax` exposed separately as attachment full-text search.
4. **Client must speak both dialects:** JSON-body POSTs and form-encoded POSTs, plus HTML-fragment parsing (selectolax) with fixture-based change detection.
5. **No auth wall anywhere** — an honest User-Agent, caching, and our own outbound throttle are matters of politeness, not necessity. All the more reason the abuse-prevention layer on *our* remote server matters: we're the ones protecting Edge from our users.
6. **Pagination pattern:** 50 rows/page (10 for `keyword`), with an exact `[Total n]` and
   `[page / pages]` count — expose `total`/`pages`/`has_more` and let callers request the
   page they want rather than looping blind.
7. Remaining open items: `tmplNm` template-name taxonomy (free-text input; values are
   harvestable from search results themselves), `cm/companySearch.ax` params, sector /
   subsector code values for `keyword/search.ax`, and the Phase 3 parse targets in §4–5.
