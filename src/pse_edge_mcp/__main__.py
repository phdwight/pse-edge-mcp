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


if __name__ == "__main__":
    main()
