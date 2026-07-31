"""The composed ASGI application, importable by an external server.

    uvicorn pse_edge_mcp.asgi:app --workers 4
    gunicorn -k uvicorn.workers.UvicornWorker pse_edge_mcp.asgi:app

Why this exists rather than only the in-process `--http` path: uvicorn's multi-worker
supervisor forks and **re-imports** the application in each child, so it needs an import
string, not an object built inside `main()`. Having the composition in one importable
place also means any ASGI server can run this, and that `__main__` and production run the
*same* stack rather than two arrangements that drift.

Composition order, outermost first — each layer's position is a decision:

    HealthApp        probes answer before anything else, and never authenticate
      AuthApp        /oauth/*, signup, /privacy, /account — reachable without a token,
                     because you cannot present one before you have one
        AuthMiddleware   bearer + quota enforcement
          MCP app        the tools themselves

**Per-process state is per worker.** The quota windows, parse memo, token cache and usage
buffer all live in memory, so N workers means a user's effective quota ceiling is up to N×
nominal and the parse memo is warmed N times. That is the same trade already accepted for
replicas (plan §6) — quotas exist to stop abuse, not to bill exactly — but it is worth
knowing before setting `--workers 16` and wondering why limits feel loose.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .health import HealthApp
from .logging_config import configure_logging
from .market_calendar import MarketCalendar
from .server import build_server, build_storage

logger = logging.getLogger(__name__)


def transport_security_for(public_url: str) -> Any:
    """Allow the host this server is actually reached at.

    The SDK guards against DNS rebinding by checking the `Host` header, and its default
    allowlist is localhost and 127.0.0.1 only. Behind any reverse proxy — Caddy, a
    Cloudflare Tunnel — the header carries the *public* hostname, so the default rejects
    every real request with `421 Invalid Host header` once OAuth has already succeeded.
    That failure is unusually hard to read: authorization completes, the client stores a
    valid token, and only the first `POST /mcp` fails.

    Derived from `PSE_PUBLIC_URL` rather than a separate variable, because that value is
    already the deployment's external identity — it drives the WebAuthn rp_id, the OAuth
    issuer and email links. A second knob for the same fact is a second thing to get wrong.

    The guard is kept, not disabled: it costs one header comparison, and turning it off
    because auth exists confuses two different attacks. Auth stops a stranger calling the
    API; this stops a browser on the victim's own network being used to reach a server
    that trusts its network position.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    parsed = urlparse(public_url)
    host = parsed.hostname or "localhost"
    # Both forms: a proxy may or may not include the port in Host, and the SDK compares
    # the header verbatim.
    hosts = {host, f"{host}:{parsed.port}" if parsed.port else host}
    if parsed.scheme == "https":
        hosts.add(f"{host}:443")
    # localhost stays allowed so container healthchecks and a local curl keep working.
    hosts |= {"localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"}
    return TransportSecuritySettings(
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted({public_url.rstrip("/"), f"{parsed.scheme}://{host}"}),
    )


def create_app(settings: Settings | None = None) -> Any:
    """Build the full production stack from configuration.

    Returns a bare ASGI callable. Startup and shutdown are handled through the lifespan
    protocol, so whichever server runs this gets the MCP session manager initialised and
    the usage buffer flushed on the way out.
    """
    settings = settings or Settings.from_env()
    configure_logging(json_output=settings.log_json, level=settings.log_level)

    storage, archive, engine = build_storage(settings)
    calendar = MarketCalendar(
        tz=settings.market_tz, open_time=settings.market_open, close_time=settings.market_close
    )
    mcp = build_server(settings, calendar=calendar, storage=storage, archive=archive)
    inner = mcp.streamable_http_app(
        json_response=not settings.sse_responses,
        stateless_http=not settings.stateful_sessions,
        transport_security=transport_security_for(settings.public_url),
    )

    app: Any = inner
    usage: Any = None

    if settings.auth_required:
        if engine is None:
            raise SystemExit(
                "PSE_AUTH_REQUIRED=1 needs DATABASE_URL — accounts live in Postgres. "
                "Unset PSE_AUTH_REQUIRED to serve without auth."
            )
        from .auth import QuotaTracker, TokenService
        from .auth_app import AuthApp
        from .auth_middleware import AuthMiddleware
        from .auth_store import PostgresAuthStore
        from .email import build_email_sender
        from .oauth import OAuthService
        from .passkeys import PasskeyService
        from .usage import UsageRecorder
        from .usage_postgres import PostgresUsageSink

        usage = UsageRecorder(
            PostgresUsageSink(engine), retention_days=settings.usage_retention_days
        )
        app = AuthMiddleware(
            app,
            TokenService(
                PostgresAuthStore(engine),
                cache_ttl_sec=settings.token_cache_ttl_sec,
                default_quota_per_minute=settings.quota_per_minute,
                default_quota_per_day=settings.quota_per_day,
            ),
            QuotaTracker(),
            resource_metadata_url=(f"{settings.public_url}/.well-known/oauth-protected-resource"),
            usage=usage,
        )
        app = AuthApp(
            app,
            oauth=OAuthService(
                engine,
                access_ttl_min=settings.access_token_ttl_min,
                refresh_ttl_days=settings.refresh_token_ttl_days,
            ),
            passkeys=PasskeyService(engine, public_url=settings.public_url),
            email=build_email_sender(settings.zeptomail_api_key, settings.email_from),
            public_url=settings.public_url,
            engine=engine,
        )
    elif settings.public_url.startswith("https://"):
        # An https deployment with auth off is almost certainly a misconfiguration rather
        # than a choice, and silently serving the world is the wrong failure mode.
        logger.warning(
            "serving over a public https URL with PSE_AUTH_REQUIRED unset — "
            "every caller is anonymous and unlimited"
        )

    app = HealthApp(app, engine=engine, version=_version())
    return _with_lifespan(app, inner, usage)


def _with_lifespan(app: Any, inner: Any, usage: Any) -> Any:
    """Wire startup/shutdown for both the MCP session manager and the usage flusher.

    The MCP app owns a task group created in *its* lifespan, so ours has to run inside
    that rather than beside it — otherwise `/mcp` fails with "task group is not
    initialized". Shutdown runs in reverse, flushing buffered usage counts before the
    database connections go away.
    """

    async def application(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            await app(scope, receive, send)
            return

        context = inner.router.lifespan_context(inner)
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                await context.__aenter__()
                if usage is not None:
                    usage.start()
            except Exception as exc:  # noqa: BLE001 - report and refuse to start
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
                return
            await send({"type": "lifespan.startup.complete"})
            message = await receive()
        if message["type"] == "lifespan.shutdown":
            if usage is not None:
                await usage.stop()
            await context.__aexit__(None, None, None)
            await send({"type": "lifespan.shutdown.complete"})

    return application


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version("pse-edge-mcp")
    except PackageNotFoundError:  # pragma: no cover - only in a source tree without install
        return "unknown"


_app: Any = None


def __getattr__(name: str) -> Any:
    """Resolve `pse_edge_mcp.asgi:app` lazily (PEP 562).

    An import string is resolved with `getattr`, so this is enough for uvicorn and
    gunicorn — while *importing* the module stays free of side effects. Building at module
    scope instead would open database connections, and raise SystemExit on a missing
    DATABASE_URL, merely because something imported it.
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
