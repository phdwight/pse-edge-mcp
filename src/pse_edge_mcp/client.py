"""PseEdgeClient — pure, MCP-agnostic access to PSE Edge's unofficial endpoints.

Protocol rules (verified by live capture, 2026-07-30):
- JSON-dialect endpoints (DisclosureCht.ax): POST with Content-Type: application/json.
  Form-encoded bodies get HTTP 415. Param names differ from the page's form fields
  (wire wants startDate/endDate).
- Form-dialect endpoints (*/search.ax): POST form-urlencoded, respond with HTML fragments.
- No cookies/session required anywhere. Dates go over the wire as MM-dd-yyyy.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings
from .errors import EdgeUnavailableError, EndpointChangedError
from .ratelimit import TokenBucket


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class PseEdgeClient:
    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None):
        self.settings = settings or Settings()
        self._http = http or httpx.AsyncClient(
            base_url=self.settings.base_url,
            headers={"User-Agent": self.settings.user_agent},
            timeout=self.settings.request_timeout_sec,
            follow_redirects=True,
        )
        self._bucket = TokenBucket(
            self.settings.throttle_rate_per_sec, self.settings.throttle_burst
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- transport helpers -------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        await self._bucket.acquire()

        @retry(
            stop=stop_after_attempt(self.settings.retry_attempts),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception(_is_transient),
            reraise=True,
        )
        async def _go() -> httpx.Response:
            resp = await self._http.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp

        try:
            return await _go()
        except httpx.HTTPStatusError as exc:
            raise EdgeUnavailableError(
                f"PSE Edge returned {exc.response.status_code} for {url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EdgeUnavailableError(f"PSE Edge unreachable: {exc}") from exc

    async def _get(self, url: str, **params: str) -> httpx.Response:
        return await self._request("GET", url, params=params or None)

    async def _post_json(self, url: str, body: dict[str, Any]) -> Any:
        resp = await self._request(
            "POST", url, json=body, headers={"Content-Type": "application/json"}
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise EndpointChangedError(f"{url}: expected JSON, got non-JSON response") from exc

    async def _post_form(self, url: str, form: dict[str, str]) -> str:
        resp = await self._request(
            "POST",
            url,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        return resp.text

    # ---- endpoints ---------------------------------------------------------

    async def search_companies(self, query: str) -> list[dict[str, Any]]:
        """GET /autoComplete/searchCompanyNameSymbol.ax — clean JSON."""
        resp = await self._get("/autoComplete/searchCompanyNameSymbol.ax", term=query)
        try:
            data = resp.json()
        except ValueError as exc:
            raise EndpointChangedError("autocomplete: non-JSON response") from exc
        if not isinstance(data, list):
            raise EndpointChangedError("autocomplete: expected a JSON array")
        return data

    async def fetch_stock_data_page(self, company_id: str) -> str:
        """GET /companyPage/stockData.do — server-rendered quote page (no JSON exists)."""
        resp = await self._get("/companyPage/stockData.do", cmpy_id=company_id)
        return resp.text

    async def fetch_price_history(
        self, company_id: str, security_id: str, start: date, end: date
    ) -> dict[str, Any]:
        """POST /common/DisclosureCht.ax (JSON dialect).

        Response: {"chartData": [{OPEN, HIGH, LOW, CLOSE, VALUE, CHART_DATE}], "tableData": [...]}
        """
        body = {
            "cmpy_id": company_id,
            "security_id": security_id,
            "startDate": start.strftime("%m-%d-%Y"),
            "endDate": end.strftime("%m-%d-%Y"),
        }
        data = await self._post_json("/common/DisclosureCht.ax", body)
        if not isinstance(data, dict) or "chartData" not in data:
            raise EndpointChangedError("DisclosureCht.ax: missing chartData key")
        return data

    # ---- disclosures (Phase 2) ---------------------------------------------

    async def search_announcements(
        self,
        *,
        from_date: date,
        to_date: date,
        company_id: str = "",
        template: str = "",
        keyword: str = "",
        page: int = 1,
    ) -> str:
        """POST /announcements/search.ax — market-wide, date-ranged disclosure list.

        The primary disclosure search: 50 rows/page, chronological (DESC), covers
        current data. `companyId` narrows it to one company. Verified 2026-07-30.
        """
        return await self._post_form(
            "/announcements/search.ax",
            {
                "pageNo": str(page),
                "keyword": keyword,
                "tmplNm": template,
                "companyId": company_id,
                "fromDate": from_date.strftime("%m-%d-%Y"),
                "toDate": to_date.strftime("%m-%d-%Y"),
                "sortType": "",
                "dateSortType": "DESC",
                "cmpySortType": "",
            },
        )

    async def search_company_disclosures(
        self, company_id: str, *, template: str = "", page: int = 1
    ) -> str:
        """POST /companyDisclosures/search.ax — one company's full disclosure history.

        `keyword` on the wire is the numeric company id (a symbol yields 0 rows).
        No date filter: use this for complete history, announcements for date ranges.
        """
        return await self._post_form(
            "/companyDisclosures/search.ax",
            {
                "pageNo": str(page),
                "keyword": company_id,
                "tmplNm": template,
                "sortType": "",
                "dateSortType": "DESC",
                "cmpySortType": "",
            },
        )

    async def search_disclosure_fulltext(
        self,
        keyword: str,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        company_id: str = "",
        subject_title: str = "",
        sector: str = "",
        subsector: str = "",
        page: int = 1,
    ) -> str:
        """POST /keyword/search.ax — full-text search over disclosure *attachment text*.

        Relevance-ordered, 10 hits/page, returns snippets. Note (verified 2026-07-30):
        Edge's full-text index is partial — it covered only 2023-2025 at capture time
        and held nothing from 2026, so this is not a substitute for
        search_announcements when recency matters. Dates go over as MM-dd-yyyy;
        startDate/startDt are silently ignored here (only fromDate/toDate filter).
        """
        return await self._post_form(
            "/keyword/search.ax",
            {
                "pageNo": str(page),
                "keyword": keyword,
                "fromDate": from_date.strftime("%m-%d-%Y") if from_date else "",
                "toDate": to_date.strftime("%m-%d-%Y") if to_date else "",
                "companyId": company_id,
                "cmpyNm": "",
                "subjectTitle": subject_title,
                "sector": sector,
                "subsector": subsector,
            },
        )

    async def fetch_disclosure_viewer(self, edge_no: str) -> str:
        """GET /openDiscViewer.do — disclosure detail page (metadata + attachment ids)."""
        resp = await self._get("/openDiscViewer.do", edge_no=edge_no)
        return resp.text
