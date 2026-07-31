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

The HTTP stack itself is composed in `asgi.py`, so this and a production
`uvicorn pse_edge_mcp.asgi:app --workers N` run exactly the same arrangement rather than
two that drift apart.
"""

from __future__ import annotations

import argparse
import dataclasses
import os

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
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="worker processes (HTTP only). Above 1 the app is re-imported per worker, so "
        "in-memory state — quota windows, parse memo, token cache — is per worker, and a "
        "user's effective quota ceiling becomes up to N x nominal.",
    )
    return parser


def with_transport_flags(settings: Settings, *, stateful: bool, sse: bool) -> Settings:
    """Apply the CLI transport switches on top of environment-derived settings.

    A flag can only turn a mode *on*: the environment is the deployment's baseline, and a
    missing flag should not silently undo `PSE_STATEFUL=1` set in a compose file.
    """
    return dataclasses.replace(
        settings,
        stateful_sessions=settings.stateful_sessions or stateful,
        sse_responses=settings.sse_responses or sse,
    )


def main() -> None:
    args = build_parser().parse_args()

    if not args.http:
        # stdio: no transport options, no auth, no HTTP stack at all.
        build_server().run()
        return

    import uvicorn

    from .asgi import create_app
    from .logging_config import configure_logging

    settings = with_transport_flags(Settings.from_env(), stateful=args.stateful, sse=args.sse)
    configure_logging(json_output=settings.log_json, level=settings.log_level)

    if args.workers > 1:
        # The multi-worker supervisor forks and re-imports, so it needs an import string —
        # an object built here could not be handed to the children. That path reads the
        # environment, so transport flags must be exported rather than passed as argv.
        if args.stateful:
            os.environ["PSE_STATEFUL"] = "1"
        if args.sse:
            os.environ["PSE_SSE"] = "1"
        uvicorn.run(
            "pse_edge_mcp.asgi:app",
            host=args.host,
            port=args.port,
            workers=args.workers,
            log_config=None,  # our JSON/plain formatter owns the output
        )
        return

    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
