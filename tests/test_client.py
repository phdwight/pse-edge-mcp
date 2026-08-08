"""Client transport tests against mocked HTTP (respx). CI never touches PSE Edge."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from pse_edge_mcp.client import PseEdgeClient
from pse_edge_mcp.config import Settings
from pse_edge_mcp.errors import EdgeUnavailableError, EndpointChangedError

BASE = "https://edge.pse.com.ph"


def make_client() -> PseEdgeClient:
    return PseEdgeClient(Settings(throttle_rate_per_sec=1000, retry_attempts=2))


@respx.mock
async def test_search_companies(autocomplete_json):
    respx.get(f"{BASE}/autoComplete/searchCompanyNameSymbol.ax").mock(
        return_value=httpx.Response(200, json=autocomplete_json)
    )
    client = make_client()
    hits = await client.search_companies("SM")
    assert hits[1]["symbol"] == "SM"
    await client.aclose()


@respx.mock
async def test_price_history_posts_json_dialect(chart_json):
    route = respx.post(f"{BASE}/common/DisclosureCht.ax").mock(
        return_value=httpx.Response(200, json=chart_json)
    )
    client = make_client()
    data = await client.fetch_price_history("599", "520", date(2026, 6, 1), date(2026, 7, 30))
    assert len(data["chartData"]) == 2

    sent = route.calls.last.request
    assert sent.headers["content-type"] == "application/json"
    body = json.loads(sent.content)
    assert body == {
        "cmpy_id": "599",
        "security_id": "520",
        "startDate": "06-01-2026",
        "endDate": "07-30-2026",
    }
    await client.aclose()


@respx.mock
async def test_shape_drift_raises_endpoint_changed():
    respx.post(f"{BASE}/common/DisclosureCht.ax").mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )
    client = make_client()
    with pytest.raises(EndpointChangedError):
        await client.fetch_price_history("599", "520", date(2026, 6, 1), date(2026, 7, 30))
    await client.aclose()


@respx.mock
async def test_server_error_becomes_edge_unavailable():
    respx.get(f"{BASE}/companyPage/stockData.do").mock(return_value=httpx.Response(503))
    client = make_client()
    with pytest.raises(EdgeUnavailableError):
        await client.fetch_stock_data_page("599")
    await client.aclose()


@respx.mock
async def test_fetch_attachment_returns_bytes_and_content_type():
    pdf = b"%PDF-1.4 tiny fixture"
    route = respx.get(f"{BASE}/downloadFile.do").mock(
        return_value=httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
    )
    raw, content_type = await make_client().fetch_attachment("1949133")
    assert raw == pdf and content_type == "application/pdf"
    assert route.calls.last.request.url.params["file_id"] == "1949133"


@respx.mock
async def test_fetch_attachment_refuses_oversized_files(monkeypatch):
    """The cap bounds cache rows and protects the politeness budget: an oversized file
    is refused before anything downstream can store it."""
    from pse_edge_mcp.errors import AttachmentTooLargeError

    monkeypatch.setattr(PseEdgeClient, "MAX_ATTACHMENT_BYTES", 16)
    respx.get(f"{BASE}/downloadFile.do").mock(
        return_value=httpx.Response(200, content=b"x" * 17)
    )
    with pytest.raises(AttachmentTooLargeError):
        await make_client().fetch_attachment("1")
