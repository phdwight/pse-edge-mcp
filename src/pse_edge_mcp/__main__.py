"""Entry point: `pse-edge-mcp` (stdio, default) or `pse-edge-mcp --http --port 8000`.

HTTP mode is **stateless with plain JSON responses by default**, which is the right shape
for this server: it is 11 read-only tools over data the freeze policy holds still, and it
uses none of the features MCP sessions exist to enable — no notifications, no resource
subscriptions, no sampling, no elicitation, no progress. Every request is self-contained.

That default is what makes horizontal scaling ordinary. Any replica can serve any request,
so plain round-robin works with no sticky routing, no per-session memory and no event store;
and without SSE, idle clients hold no connection, so N users stop meaning N concurrent
connections. All shared state already lives in Postgres (Phase 4), so this is the last
piece of "any replica, any request".

`--stateful` and `--sse` restore session mode for anyone who needs resumability or
server-initiated messages. They are independent: statelessness is the scaling property,
plain JSON is what keeps ordinary proxies and autoscalers happy.
"""

from __future__ import annotations

import argparse

from .config import Settings
from .server import build_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pse-edge-mcp", description="PSE Edge MCP server")
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--stateful",
        action="store_true",
        help="keep MCP sessions (Mcp-Session-Id) instead of the stateless default. Needed "
        "only for resumability or server-initiated messages, neither of which this server "
        "uses; it also forces sticky routing behind a load balancer.",
    )
    parser.add_argument(
        "--sse",
        action="store_true",
        help="stream responses as server-sent events instead of plain JSON",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()

    if args.http and settings.auth_required:
        _serve_http_with_auth(args, settings)
        return

    mcp = build_server()
    if args.http:
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            stateless_http=not args.stateful,
            json_response=not args.sse,
        )
    else:
        mcp.run()


def _serve_http_with_auth(args: argparse.Namespace, settings: Settings) -> None:
    """HTTP with bearer auth + quotas enforced ahead of the MCP app (PSE_AUTH_REQUIRED=1).

    Everything Postgres-flavoured is imported lazily here: this branch requires the
    `postgres` extra, and installs without it must never pay an ImportError for a mode
    they did not enable.
    """
    if not settings.database_url:
        raise SystemExit(
            "PSE_AUTH_REQUIRED=1 needs DATABASE_URL — accounts live in Postgres. "
            "Unset PSE_AUTH_REQUIRED to serve without auth."
        )

    import asyncio

    import uvicorn

    from .auth import QuotaTracker, TokenService
    from .auth_app import AuthApp
    from .auth_middleware import AuthMiddleware
    from .auth_store import PostgresAuthStore, check_auth_schema
    from .email import build_email_sender
    from .oauth import OAuthService
    from .passkeys import PasskeyService
    from .server import build_storage

    storage, archive, engine = build_storage(settings)
    if engine is None:  # unreachable given the database_url check; keeps mypy honest
        raise SystemExit("DATABASE_URL did not produce a database engine")

    async def _preflight() -> None:
        await check_auth_schema(engine)
        # Connections opened during the check are bound to this throwaway event loop;
        # dispose so uvicorn's loop starts the pool from scratch.
        await engine.dispose()

    asyncio.run(_preflight())

    mcp = build_server(settings, storage=storage, archive=archive)
    app = mcp.streamable_http_app(
        json_response=not args.sse,
        stateless_http=not args.stateful,
        host=args.host,
    )
    tokens = TokenService(
        PostgresAuthStore(engine),
        cache_ttl_sec=settings.token_cache_ttl_sec,
        default_quota_per_minute=settings.quota_per_minute,
        default_quota_per_day=settings.quota_per_day,
    )

    # Layering matters here. The bearer middleware wraps only the MCP app, and the OAuth
    # surface wraps *that* — so /oauth/* and the signup pages are reachable without a
    # token (you cannot present one before you have one), while everything else still
    # requires it. `/.well-known/*` is additionally exempt inside the middleware so
    # discovery works even if the ordering is ever changed.
    guarded = AuthMiddleware(
        app,
        tokens,
        QuotaTracker(),
        resource_metadata_url=f"{settings.public_url}/.well-known/oauth-protected-resource",
    )
    surface = AuthApp(
        guarded,
        oauth=OAuthService(
            engine,
            access_ttl_min=settings.access_token_ttl_min,
            refresh_ttl_days=settings.refresh_token_ttl_days,
        ),
        passkeys=PasskeyService(engine, public_url=settings.public_url),
        email=build_email_sender(settings.zeptomail_api_key, settings.email_from),
        public_url=settings.public_url,
    )
    uvicorn.run(surface, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
