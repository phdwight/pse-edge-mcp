"""Structured errors surfaced through MCP tool results."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class PseEdgeMcpError(Exception):
    """Base class; `code` is machine-readable for MCP clients."""

    code = "INTERNAL_ERROR"

    def payload(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self)}


class SymbolNotFoundError(PseEdgeMcpError):
    code = "SYMBOL_NOT_FOUND"


class InvalidArgumentError(PseEdgeMcpError):
    """Caller-supplied argument is malformed — rejected before any upstream request."""

    code = "INVALID_ARGUMENT"


class EndpointChangedError(PseEdgeMcpError):
    """PSE Edge responded, but not in the shape we recorded. Loud by design."""

    code = "ENDPOINT_CHANGED"


class EdgeUnavailableError(PseEdgeMcpError):
    code = "EDGE_UNAVAILABLE"


class MarketOpenNoCacheError(PseEdgeMcpError):
    """Strict freeze policy: no upstream fetches while the market is open.

    Not raised since 0.13.0 — an uncached price read during the session now falls back
    to a one-time fetch served with `stale: true` and an explanatory `meta.note`. The
    class stays so existing clients that handle the code keep compiling; removing it is
    an API decision, not a refactor.
    """

    code = "MARKET_OPEN_NO_CACHE"

    def __init__(self, message: str, retry_after: datetime):
        super().__init__(message)
        self.retry_after = retry_after

    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "retry_after": self.retry_after.isoformat()}


class ActionUnavailableError(PseEdgeMcpError):
    """An action tool cannot run in this deployment or session.

    Distinct from INVALID_ARGUMENT: the arguments may be perfect and the action still
    unavailable — no authenticated caller (stdio has none by design), or the operator has
    not configured the capability. A client should stop asking rather than retry.
    """

    code = "ACTION_UNAVAILABLE"


class ActionRateLimitedError(PseEdgeMcpError):
    """An action tool's own budget is spent, separately from the HTTP request quota.

    Same `RATE_LIMITED` code the middleware uses at the HTTP layer, deliberately: a client
    already knows that vocabulary, and "you may read but not send right now" is the same
    kind of answer whichever layer decided it.
    """

    code = "RATE_LIMITED"

    def __init__(self, message: str, retry_after_seconds: int):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "retry_after_seconds": self.retry_after_seconds}
