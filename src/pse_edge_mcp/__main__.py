"""Entry point: `pse-edge-mcp` (stdio, default) or `pse-edge-mcp --http --port 8000`."""

from __future__ import annotations

import argparse

from .server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="pse-edge-mcp", description="PSE Edge MCP server")
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    mcp = build_server()
    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
