"""BspClient — pure, MCP-agnostic access to Bangko Sentral ng Pilipinas (BSP) data.

A second upstream, separate from PSE Edge, so it is a separate client with its own base
URL — the same reason `sources.py` keeps one narrow protocol per domain: the Key Rates
repository must not be able to reach a PSE Edge endpoint, and vice versa.

Protocol notes (verified by live capture, 2026-10-04):
- The public Key Rates dashboard (SitePages/Statistics/KeyRates.aspx) is Angular + PnP.js;
  the figures are not in the server-rendered HTML. The page queries a SharePoint list
  titled "Key Rates" anonymously via the SharePoint REST API, which we hit directly:

      GET /_api/web/lists/getByTitle('Key Rates')/items?$select=*&$orderby=Order0

  with `Accept: application/json;odata=nometadata` for a flat `{"value": [...]}` envelope
  (no verbose OData metadata). No cookies, auth, or X-RequestDigest needed for this read.
- **BSP's WAF fingerprints clients.** A browser User-Agent on a non-browser TLS stack is
  403'd; our honest bot UA is served HTTP 200. Do NOT spoof a browser here.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .client import _is_transient
from .config import Settings
from .errors import EdgeUnavailableError, EndpointChangedError
from .ratelimit import TokenBucket

#: The SharePoint list the public dashboard binds to. Spaces and the quoting are exactly
#: what PnP emits for `getByTitle("Key Rates")`; `$orderby=Order0` mirrors the page's own
#: `.orderBy('Order0', true)` so rows arrive in display order.
KEY_RATES_PATH = (
    "/_api/web/lists/getByTitle('Key Rates')/items?$select=*&$orderby=Order0"
)


class BspClient:
    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None):
        self.settings = settings or Settings()
        self._http = http or httpx.AsyncClient(
            base_url=self.settings.bsp_base_url,
            headers={"User-Agent": self.settings.user_agent},
            timeout=self.settings.request_timeout_sec,
            follow_redirects=True,
        )
        self._bucket = TokenBucket(
            self.settings.throttle_rate_per_sec, self.settings.throttle_burst
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_json(self, url: str, **params: str) -> Any:
        await self._bucket.acquire()

        @retry(
            stop=stop_after_attempt(self.settings.retry_attempts),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception(_is_transient),
            reraise=True,
        )
        async def _go() -> httpx.Response:
            # nometadata keeps the envelope flat (`{"value": [...]}`); the default verbose
            # OData wraps every row in `__metadata`, which we would only strip anyway.
            resp = await self._http.get(
                url,
                params=params or None,
                headers={"Accept": "application/json;odata=nometadata"},
            )
            resp.raise_for_status()
            return resp

        try:
            resp = await _go()
        except httpx.HTTPStatusError as exc:
            raise EdgeUnavailableError(
                f"BSP returned {exc.response.status_code} for {url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EdgeUnavailableError(f"BSP unreachable: {exc}") from exc

        try:
            return resp.json()
        except ValueError as exc:
            raise EndpointChangedError(f"{url}: expected JSON, got non-JSON response") from exc

    async def fetch_key_rates(self) -> list[dict[str, Any]]:
        """GET the "Key Rates" SharePoint list — the public dashboard's own data source.

        Returns the raw list rows (policy/facility rates, BSP-securities and TDF WAIRs,
        headline inflation, and the USD peso reference). Turning these into meaning is
        `parsers.parse_key_rates`' job; this is the transport boundary.
        """
        data = await self._get_json(KEY_RATES_PATH)
        value = data.get("value") if isinstance(data, dict) else None
        if not isinstance(value, list):
            raise EndpointChangedError("BSP Key Rates: missing 'value' array")
        return value
