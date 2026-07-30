"""Structured errors surfaced through MCP tool results."""

from __future__ import annotations

from datetime import datetime


class PseEdgeMcpError(Exception):
    """Base class; `code` is machine-readable for MCP clients."""

    code = "INTERNAL_ERROR"

    def payload(self) -> dict:
        return {"error": self.code, "message": str(self)}


class SymbolNotFoundError(PseEdgeMcpError):
    code = "SYMBOL_NOT_FOUND"


class EndpointChangedError(PseEdgeMcpError):
    """PSE Edge responded, but not in the shape we recorded. Loud by design."""

    code = "ENDPOINT_CHANGED"


class EdgeUnavailableError(PseEdgeMcpError):
    code = "EDGE_UNAVAILABLE"


class MarketOpenNoCacheError(PseEdgeMcpError):
    """Strict freeze policy: no upstream fetches while the market is open."""

    code = "MARKET_OPEN_NO_CACHE"

    def __init__(self, message: str, retry_after: datetime):
        super().__init__(message)
        self.retry_after = retry_after

    def payload(self) -> dict:
        return {**super().payload(), "retry_after": self.retry_after.isoformat()}
