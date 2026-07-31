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
from pathlib import Path
from typing import Any

import httpx
import pytest

from pse_edge_mcp.config import Settings
from pse_edge_mcp.health import HealthApp
from pse_edge_mcp.logging_config import JsonFormatter, configure_logging, redact

REPO_ROOT = Path(__file__).resolve().parent.parent


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
