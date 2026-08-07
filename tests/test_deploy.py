"""Deployment surface: health probes, structured logging, and the ASGI factory.

These pin behaviours an orchestrator depends on. The liveness/readiness split in
particular has a failure mode worth guarding: a liveness probe that touches the database
restarts every replica during a database blip, turning a recoverable outage into an outage
plus a restart storm.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from pse_edge_mcp.config import Settings
from pse_edge_mcp.health import HealthApp
from pse_edge_mcp.logging_config import (
    _PLAIN_FORMAT,
    _TIME_FORMAT,
    JsonFormatter,
    PlainFormatter,
    configure_logging,
    redact,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def app_lifespan(app):
    """httpx's ASGITransport does not run the lifespan, and the MCP session manager's task
    group is created there — without this, /mcp fails with "task group is not initialized"."""
    done = anyio.Event()
    started = anyio.Event()

    async def run():
        async def receive():
            if not started.is_set():
                return {"type": "lifespan.startup"}
            await done.wait()
            return {"type": "lifespan.shutdown"}

        async def send(message):
            if message["type"] == "lifespan.startup.complete":
                started.set()

        await app({"type": "lifespan"}, receive, send)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await started.wait()
        try:
            yield
        finally:
            done.set()


class Inner:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": [(b"x-inner", b"1")]})
        await send({"type": "http.response.body", "body": b"inner"})


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- health ------------------------------------------------------------------


async def test_liveness_never_touches_the_database():
    """A liveness probe that depends on Postgres restarts every replica during a blip."""

    class ExplodingEngine:
        def connect(self):
            raise AssertionError("liveness must not open a database connection")

    app = HealthApp(Inner(), engine=ExplodingEngine(), version="9.9.9")
    async with client(app) as http:
        response = await http.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "9.9.9"
    assert body["uptime_seconds"] >= 0
    assert response.headers["cache-control"] == "no-store", "a cached probe hides a dead replica"


async def test_readiness_reports_503_when_the_database_is_unreachable(pg_engine=None):
    """503, not 500: 'do not route to me yet' is a normal state, not a bug."""
    from pse_edge_mcp.db import create_engine

    dead = create_engine("postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing")
    try:
        app = HealthApp(Inner(), engine=dead)
        async with client(app) as http:
            response = await http.get("/health/ready")
    finally:
        await dead.dispose()

    assert response.status_code == 503
    assert response.json()["database"] == "unreachable"


async def test_readiness_is_ok_without_a_database():
    """No DATABASE_URL is a valid deployment (in-memory cache), not a fault."""
    async with client(HealthApp(Inner(), engine=None)) as http:
        response = await http.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["database"] == "not configured"


async def test_probes_never_reach_the_wrapped_app_and_everything_else_does():
    inner = Inner()
    async with client(HealthApp(inner)) as http:
        await http.get("/health")
        await http.get("/health/ready")
        assert inner.calls == 0
        passthrough = await http.get("/mcp")

    assert passthrough.headers["x-inner"] == "1"
    assert inner.calls == 1


@pytest.mark.postgres
async def test_readiness_is_ok_against_a_live_database(pg_engine):
    async with client(HealthApp(Inner(), engine=pg_engine)) as http:
        response = await http.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


# --- compose files -----------------------------------------------------------

# `!reset`, `!override` and friends are Docker-Compose-only YAML tags. A conforming YAML
# parser rejects an unknown tag outright — which is what NAS Docker UIs, PaaS importers and
# editor linters do, and they were reported failing on exactly this. Compose itself is happy,
# so nothing here catches it: the file is only broken where it is deployed.
_CUSTOM_YAML_TAG = re.compile(r"(?:^|[\s:\-])(![a-zA-Z_][\w]*)")


@pytest.mark.parametrize("path", sorted(REPO_ROOT.glob("compose*.yaml")), ids=lambda p: p.name)
def test_compose_files_use_no_compose_only_yaml_tags(path):
    found = {m.group(1) for m in _CUSTOM_YAML_TAG.finditer(path.read_text())}
    assert not found, (
        f"{path.name} uses Compose-only YAML tag(s) {sorted(found)}. Portable parsers reject "
        f"unknown tags, so the file breaks in NAS/PaaS importers while `docker compose` "
        f"accepts it. Restructure so nothing needs undoing — note that `ports` merges "
        f"additively, so an overlay can add a mapping but never remove one."
    )


# --- structured logging ------------------------------------------------------


def test_json_formatter_emits_one_parseable_object_per_record():
    record = logging.LogRecord(
        name="pse",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="served %s",
        args=("/mcp",),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "pse"
    assert payload["message"] == "served /mcp"
    assert "timestamp" in payload


def test_extra_fields_ride_along_as_their_own_keys():
    """The point of structured logs: get at a user id without parsing the message."""
    record = logging.LogRecord(
        name="pse",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="quota exceeded",
        args=(),
        exc_info=None,
    )
    record.user_id = "u-123"  # what logger.warning(..., extra={"user_id": ...}) produces
    payload = json.loads(JsonFormatter().format(record))
    assert payload["user_id"] == "u-123"


@pytest.mark.parametrize(
    "text",
    [
        "token pse_AbCdEf0123456789AbCdEf0123456789",
        "Authorization: Bearer sk-supersecretvalue",
        "authorization=hunter2hunter2",
        "sending with Zoho-enczapikey wSsVR60lSECRETKEYVALUE==",
        "api_key: abcd1234",
    ],
)
def test_secrets_are_redacted_as_a_backstop(text):
    """Nothing in this codebase logs a credential; this catches anything that slips into a
    message by accident."""
    cleaned = redact(text)
    assert "[redacted]" in cleaned
    for leak in ("pse_AbCdEf0123456789", "supersecret", "hunter2", "wSsVR60l", "abcd1234"):
        assert leak not in cleaned


def test_configure_logging_is_idempotent():
    """Called by both the CLI and the factory; repeated calls must not double every line."""
    configure_logging(json_output=True)
    configure_logging(json_output=True)
    ours = [h for h in logging.getLogger().handlers if getattr(h, "name", None) == "pse-edge-mcp"]
    assert len(ours) == 1, "repeated calls must not stack duplicate handlers"
    configure_logging(json_output=False, level="DEBUG")  # restore a readable default


def test_configure_logging_leaves_other_handlers_alone():
    """Clearing every root handler would be idempotent by stomping on pytest's capture, or
    on a host application that embedded this server."""
    foreign = logging.StreamHandler()
    foreign.set_name("someone-elses-handler")
    logging.getLogger().addHandler(foreign)
    try:
        configure_logging(json_output=False)
        assert foreign in logging.getLogger().handlers
    finally:
        logging.getLogger().removeHandler(foreign)


# --- the ASGI factory --------------------------------------------------------


def test_importing_the_module_builds_nothing():
    """Building at module scope would open connections, and SystemExit on a missing
    DATABASE_URL, merely because something imported it."""
    import pse_edge_mcp.asgi as asgi

    assert asgi._app is None


async def test_factory_serves_health_and_mcp_without_a_database():
    """The zero-config HTTP deployment: no Postgres, no auth, still healthy."""
    from pse_edge_mcp.asgi import create_app

    app = create_app(Settings(throttle_rate_per_sec=1000))
    async with client(app) as http:
        health = await http.get("/health")
        ready = await http.get("/health/ready")

    assert health.status_code == 200
    assert ready.json()["database"] == "not configured"


def test_factory_refuses_auth_without_a_database():
    """Fail at startup with an actionable message rather than at the first request."""
    from pse_edge_mcp.asgi import create_app

    with pytest.raises(SystemExit, match="DATABASE_URL"):
        create_app(Settings(auth_required=True, database_url=None))


def test_https_without_auth_warns_loudly(caplog):
    """Almost certainly a misconfiguration; serving the world silently is the wrong
    failure mode."""
    from pse_edge_mcp.asgi import create_app

    with caplog.at_level(logging.WARNING):
        create_app(Settings(public_url="https://mcp.example.com", throttle_rate_per_sec=1000))

    assert any("anonymous and unlimited" in record.message for record in caplog.records)


def test_server_advertises_its_version_in_serverinfo():
    """It shipped as an empty string: `version` was never passed, and the SDK defaults it.
    Every initialize response carries serverInfo, so this is the first thing a connecting
    client is told about the server."""
    from pse_edge_mcp.server import build_server

    server = build_server(Settings(throttle_rate_per_sec=1000))
    version = getattr(server, "version", None)

    assert version, "serverInfo.version must not be empty"
    assert version[0].isdigit(), f"expected a release version, got {version!r}"


# --- transport security ------------------------------------------------------


def test_the_public_host_is_allowed_by_the_dns_rebinding_guard():
    """Production shipped with the SDK's default allowlist — localhost and 127.0.0.1 only.

    Behind any reverse proxy the Host header carries the public hostname, so every real
    request was refused with `421 Invalid Host header` *after* OAuth had fully succeeded:
    the client held a valid token and only the first POST /mcp failed. The old journey test
    passed the setting explicitly, so it exercised a configuration production never built.
    """
    from pse_edge_mcp.asgi import transport_security_for

    security = transport_security_for("https://pse.example.com")

    assert "pse.example.com" in security.allowed_hosts
    assert "pse.example.com:443" in security.allowed_hosts, "a proxy may include the port"
    assert "https://pse.example.com" in security.allowed_origins
    assert "localhost" in security.allowed_hosts, "healthchecks call 127.0.0.1 directly"


async def test_a_proxied_request_with_the_public_host_reaches_the_mcp_app():
    """End to end through the real factory: the Host header a proxy sends must not 421."""
    from pse_edge_mcp.asgi import create_app

    app = create_app(Settings(public_url="https://pse.example.com", throttle_rate_per_sec=1000))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://pse.example.com"
    ) as http:
        async with app_lifespan(app):
            response = await http.post(
                "/mcp",
                headers={"accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "1"},
                    },
                },
            )

    assert response.status_code != 421, (
        f"DNS-rebinding guard refused the public host: {response.text}"
    )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["version"], "serverInfo.version was empty"


# --- timestamps and critical-path logging ------------------------------------


def test_plain_log_lines_carry_an_iso_timestamp():
    """The JSON formatter always had one; the plain formatter did not — so the format a
    developer reads in a terminal, and the one a deployment without PSE_LOG_JSON writes,
    produced lines that could not be correlated with anything."""
    record = logging.LogRecord(
        name="pse",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="upstream: fetching",
        args=(),
        exc_info=None,
    )
    line = PlainFormatter(_PLAIN_FORMAT, datefmt=_TIME_FORMAT).format(record)

    stamp = line.split()[0]
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    assert parsed.tzinfo is not None, "an offset is what makes the stamp unambiguous"
    assert "INFO" in line and "upstream: fetching" in line


def test_the_plain_formatter_redacts_too():
    """Otherwise the safer-looking format is the leakier one."""
    record = logging.LogRecord(
        name="pse",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="client_secret=hunter2hunter2",
        args=(),
        exc_info=None,
    )
    line = PlainFormatter(_PLAIN_FORMAT, datefmt=_TIME_FORMAT).format(record)
    assert "hunter2" not in line
    assert "[redacted]" in line


async def test_every_upstream_fetch_is_logged(caplog):
    """The one log line this server exists to keep rare. If it appears during market
    hours the freeze invariant is broken, so it has to be visible per fetch."""
    from pse_edge_mcp.market_calendar import MarketCalendar
    from pse_edge_mcp.service import FreezeService

    class Closed(MarketCalendar):
        def is_market_open(self, dt=None) -> bool:
            return False

    service = FreezeService(calendar=Closed())
    with caplog.at_level(logging.INFO, logger="pse_edge_mcp.service"):
        await service.get("quote:SM", lambda: _answer("value"))
        await service.get("quote:SM", lambda: _answer("value"))  # cache hit, no fetch

    fetches = [r for r in caplog.records if "upstream: fetching" in r.message]
    assert len(fetches) == 1, "one line per real fetch, and none for a cache hit"
    assert "quote:SM" in fetches[0].message
    assert any("duration_ms" in r.message for r in caplog.records)


async def test_an_uncached_session_price_fetch_is_logged_and_flagged(caplog):
    """The freeze's one exception must be visible per occurrence: an operator watching
    the logs should see exactly why an EOD-frozen fetch happened during market hours."""
    from pse_edge_mcp.market_calendar import MarketCalendar
    from pse_edge_mcp.service import FreezeService

    class Open(MarketCalendar):
        def is_market_open(self, dt=None) -> bool:
            return True

    service = FreezeService(calendar=Open())
    with caplog.at_level(logging.INFO, logger="pse_edge_mcp.service"):
        served = await service.get("quote:SM", lambda: _answer("session-value"))

    assert any("fetching once" in r.message for r in caplog.records)
    assert served.meta.stale is True and served.meta.note, "served, but labelled non-realtime"


async def _answer(value):
    return value


def test_startup_logs_the_resolved_configuration(caplog):
    """ "What config is this actually running?" is the first question in any incident, and
    answering it from env vars and compose files is guesswork."""
    from pse_edge_mcp.asgi import create_app

    with caplog.at_level(logging.INFO, logger="pse_edge_mcp.asgi"):
        create_app(Settings(public_url="http://localhost:8000", throttle_rate_per_sec=1000))

    line = next(r.message for r in caplog.records if r.message.startswith("starting pse-edge-mcp"))
    for field in ("public_url=", "auth=", "storage=", "transport=", "email=", "market="):
        assert field in line, f"{field} missing from the startup summary"
    assert "auth=OFF" in line, "an auth-less deployment must be obvious in the log"
