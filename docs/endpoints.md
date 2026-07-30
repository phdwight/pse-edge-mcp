# PSE Edge Endpoint Recon — Phase 0 Report (v2, post browser capture)

**Date:** 2026-07-30
**Base URL:** `https://edge.pse.com.ph`
**Method:** live probes from cloud (US IP), community source-mining, and **live browser network capture + request replay** (Chrome, Manila IP).

**Legend:** ✅ VERIFIED (live, request+response confirmed) · 📖 COMMUNITY (from working OSS code, not re-verified) · ⚠️ EXISTS (endpoint confirmed live, params need fixture work)

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

### ✅ `POST /companyDisclosures/search.ax` (form style) — **fully verified end-to-end**
Params: `pageNo`, `keyword` (company id or free text), `tmplNm` (template name filter, free text), `sortType`, `dateSortType` (`DESC`), `cmpySortType`. Returns HTML: **50 rows/page**, row cells = template name (e.g. `[Amend-1]Amendments to Articles…`), announce datetime, PSE form no, circular no. Each row: `openPopup('<32-hex edge_no>')`. Pagination via `goPage(n)` (SM had 7+ pages). No total-count text — detect last page by short row count.

### ✅ `POST /keyword/search.ax` (form style) — advanced/market-wide search
From `/keyword/form.do`. Params observed on form: `keyword`, `fromDate`, `toDate`, `companyId`, `cmpyNm`, `subjectTitle`, `sector`, `subsector`, `pageNo`. **This is the date-range disclosure search** → primary backend for `search_disclosures`. Fixture pass needed for response shape.

### ⚠️ `POST /announcements/search.ax` (form style)
Exists (200). Params mirror companyDisclosures plus `fromDate`/`toDate`; empty-param replay returned 0 rows — needs param experimentation.

### ✅ `GET /openDiscViewer.do?edge_no={32-hex}`
Disclosure detail page: company, template, announce date, subject, attachments. Contains:
- **`GET /downloadHtml.do?file_id={int}`** — rendered HTML of the disclosure body (iframe preview)
- **`GET /downloadFile.do?file_id={int}`** — actual attachment download (PDF etc.; the page's Download button submits a GET form with `file_id`)

`edge_no` = stable disclosure natural key. One disclosure can have multiple `file_id`s (body + attachments).

## 4. Financial reports & company info

### 📖 `GET /companyPage/financial_reports_view.do?cmpy_id={id}`
Four HTML tables (BS/IS × annual/quarterly). Fields as previously documented; negatives rendered `(1,234)`. → `get_financial_highlights`.

### ✅ `GET /companyInformation/form.do?cmpy_id={id}` → `get_company_profile`
### ✅ `GET /companyPage/dividends_and_rights_form.do?cmpy_id={id}` → `get_dividends_and_rights` (parse TBD)
### ✅ `GET /companyPage/directors_and_management_list.do?cmpy_id={id}` (parse TBD)

## 5. Indices & market-wide

### ✅ `GET /` — indices are **server-rendered HTML** (no AJAX feed exists)
Table: Index / Value / Chg / %Chg for PSEi, All Shares, Financials, Industrial, Holding Firms, Property, Services, Mining and Oil (+▲▼ direction). → `get_indices` parses this (or `/index/form.do` for detail).
**Confirmed: no gainers/losers/most-active section exists on Edge.** Drop from v1 scope (PSE's main site, not Edge, would be the source if ever wanted).

Homepage also carries server-rendered: recent disclosures, financial reports feed, dividends & rights, halts & suspensions, most-viewed disclosures — all parseable for `get_market_summary`.

### Market-wide pages (existence ✅, parse TBD): `/index/form.do`, `/psei/form.do`, `/disclosureData/dividends_and_rights_info_form.do`, `/disclosureData/halts_and_suspensions_list.do`, `/disclosureData/listing_applicants_list.do`, `/disclosureData/etf_form.do`, `/disclosureNotices/form.do`, `/listingNotices/form.do`, `/otherReports/form.do`, `/companyPage/marketCalendar.do`

## 6. Implications for the build

1. **`get_stock_quote` is an HTML parse**, not JSON — slightly more Phase 1 parser work; recorded HTML fixtures are essential.
2. **`security_id` resolution:** parse it straight off `stockData.do` per company (simpler than directory pagination); directory sync still populates the Postgres `companies` table.
3. **`search_disclosures` maps to `/keyword/search.ax`** (date range + sector filters), with `companyDisclosures/search.ax` as the per-company variant.
4. **Client must speak both dialects:** JSON-body POSTs and form-encoded POSTs, plus HTML-fragment parsing (selectolax) with fixture-based change detection.
5. **No auth wall anywhere** — an honest User-Agent, caching, and our own outbound throttle are matters of politeness, not necessity. All the more reason the abuse-prevention layer on *our* remote server matters: we're the ones protecting Edge from our users.
6. **Pagination pattern:** 50 rows/page, no total count → iterate until short page.
7. Minor open items for Phase 1–2 fixtures: `tmplNm` template-name taxonomy (free-text input; likely values harvestable from search results themselves), `announcements/search.ax` required params, `cm/companySearch.ax` params, response shape of `keyword/search.ax`.
