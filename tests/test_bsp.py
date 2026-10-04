"""BSP key rates: client transport, parser, repository routing, and the two tools.

The client runs against mocked HTTP (respx); the parser runs against the recorded
fixture; the repository runs against fakes. No test touches BSP.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from pse_edge_mcp.bsp_client import KEY_RATES_PATH, BspClient
from pse_edge_mcp.config import Settings
from pse_edge_mcp.errors import EdgeUnavailableError, EndpointChangedError
from pse_edge_mcp.models import Meta
from pse_edge_mcp.parsers import parse_key_rates
from pse_edge_mcp.repositories import KeyRatesRepository
from pse_edge_mcp.service import Served

BSP = "https://www.bsp.gov.ph"
MNL = ZoneInfo("Asia/Manila")
AS_OF = datetime(2026, 10, 4, 11, 0, tzinfo=MNL)


class FakeCache:
    """A FrozenCache that always fetches and records how it was keyed."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.policies: list[str] = []
        self.fetches = 0

    async def get(self, key: str, fetch: Any, *, policy: str = "EOD-frozen") -> Served[Any]:
        self.keys.append(key)
        self.policies.append(policy)
        self.fetches += 1
        return Served(
            value=await fetch(),
            meta=Meta(as_of=AS_OF, valid_until=AS_OF, from_cache=False, data_policy=policy),  # type: ignore[arg-type]
        )


class FakeKeyRatesSource:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = 0

    async def fetch_key_rates(self) -> list[dict[str, Any]]:
        self.calls += 1
        return self.rows


def make_client() -> BspClient:
    return BspClient(Settings(throttle_rate_per_sec=1000, retry_attempts=2))


# ---- client (transport) ------------------------------------------------------


@respx.mock
async def test_fetch_key_rates_requests_nometadata_json(bsp_key_rates_json):
    route = respx.get(f"{BSP}{KEY_RATES_PATH.split('?')[0]}").mock(
        return_value=httpx.Response(200, json=bsp_key_rates_json)
    )
    client = make_client()
    rows = await client.fetch_key_rates()
    assert len(rows) == 12
    # The flat envelope is deliberate: ask SharePoint for nometadata so we parse `value`
    # directly rather than peeling `d.results`/`__metadata`.
    assert route.calls.last.request.headers["accept"] == "application/json;odata=nometadata"
    await client.aclose()


@respx.mock
async def test_missing_value_envelope_raises_endpoint_changed():
    respx.get(f"{BSP}{KEY_RATES_PATH.split('?')[0]}").mock(
        return_value=httpx.Response(200, json={"not_value": []})
    )
    client = make_client()
    with pytest.raises(EndpointChangedError):
        await client.fetch_key_rates()
    await client.aclose()


@respx.mock
async def test_non_json_raises_endpoint_changed():
    respx.get(f"{BSP}{KEY_RATES_PATH.split('?')[0]}").mock(
        return_value=httpx.Response(200, text="<html>WAF challenge</html>")
    )
    client = make_client()
    with pytest.raises(EndpointChangedError):
        await client.fetch_key_rates()
    await client.aclose()


@respx.mock
async def test_http_error_maps_to_edge_unavailable():
    respx.get(f"{BSP}{KEY_RATES_PATH.split('?')[0]}").mock(return_value=httpx.Response(503))
    client = make_client()
    with pytest.raises(EdgeUnavailableError):
        await client.fetch_key_rates()
    await client.aclose()


# ---- parser ------------------------------------------------------------------


def test_parse_key_rates_percent_and_passthrough(bsp_key_rates_json):
    by_name = {r["name"]: r for r in parse_key_rates(bsp_key_rates_json["value"])}

    target = by_name["Target RRP Rate"]
    assert target["value"] == "5.00%"
    assert target["rate_percent"] == 5.0

    # The FX reference is a peso level, not a percentage — never coerced into a 'rate'.
    assert by_name["US$ 1.00"]["rate_percent"] is None
    # The discontinued ON Reference Rate publishes '****'.
    assert by_name["ON Reference Rate"]["rate_percent"] is None
    # Accepted yields ride along only where BSP reports them (the ON RRP auction row).
    assert by_name["ON RRP Rate"]["accepted_yields"] is not None


def test_parse_key_rates_drift_on_missing_fields():
    with pytest.raises(EndpointChangedError):
        parse_key_rates([{"Order0": 1.0}])  # no Title/Value


def test_parse_key_rates_empty_is_drift():
    with pytest.raises(EndpointChangedError):
        parse_key_rates([])


# ---- repository --------------------------------------------------------------


def _repo(rows: list[dict[str, Any]]) -> tuple[KeyRatesRepository, FakeCache, FakeKeyRatesSource]:
    cache = FakeCache()
    source = FakeKeyRatesSource(rows)
    return KeyRatesRepository(source, cache, BSP), cache, source


async def test_key_rates_uses_daily_refresh_and_absolutises_urls(bsp_key_rates_json):
    repo, cache, _ = _repo(bsp_key_rates_json["value"])
    served = await repo.key_rates()

    assert cache.keys == ["bsp:key_rates"]
    # Not the EOD-frozen default: BSP has no PSE trading session.
    assert cache.policies == ["daily-refresh"]
    assert len(served.value.rates) == 12
    assert all(r.source_url.startswith(f"{BSP}/") for r in served.value.rates)


async def test_policy_rate_projects_the_corridor(bsp_key_rates_json):
    repo, _, _ = _repo(bsp_key_rates_json["value"])
    policy = (await repo.policy_rate()).value

    assert policy.policy_rate_percent == 5.0
    assert policy.lending_rate_percent == 5.5  # ceiling
    assert policy.deposit_rate_percent == 4.5  # floor
    assert policy.published_date == "10/02/2026"
    assert [r.name for r in policy.corridor] == [
        "ON Lending Rate",
        "Target RRP Rate",
        "ON Deposit Rate",
    ]


async def test_policy_rate_drift_when_target_rrp_missing(bsp_key_rates_json):
    rows = [r for r in bsp_key_rates_json["value"] if r["Title"] != "Target RRP Rate"]
    repo, _, _ = _repo(rows)
    with pytest.raises(EndpointChangedError):
        await repo.policy_rate()


async def test_shared_memo_parses_once_across_projections(bsp_key_rates_json):
    from pse_edge_mcp.memo import ParsedMemo

    cache = FakeCache()
    source = FakeKeyRatesSource(bsp_key_rates_json["value"])
    # One real memo keyed by meta.as_of: a second read of the same cached rows reuses the
    # parsed value instead of re-parsing (see memo.py).
    memo = ParsedMemo()
    repo = KeyRatesRepository(source, cache, BSP, memo)

    await repo.key_rates()
    await repo.key_rates()
    assert memo.stats()["misses"] == 1  # parsed once; second read is a memo hit
