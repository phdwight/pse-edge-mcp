"""Runtime configuration, sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time

USER_AGENT = "pse-edge-mcp/0.1 (+https://github.com/phdwight/pse-edge-mcp; polite EOD-only client)"


@dataclass(frozen=True)
class Settings:
    """All tunables in one place. Env vars override defaults."""

    base_url: str = "https://edge.pse.com.ph"
    user_agent: str = USER_AGENT

    # Market session (Asia/Manila). Boundaries drive the cache freeze policy.
    market_tz: str = "Asia/Manila"
    market_open: time = field(default=time(9, 30))
    market_close: time = field(default=time(15, 0))

    # Outbound politeness throttle (protects PSE Edge; independent of user quotas).
    throttle_rate_per_sec: float = 1.0
    throttle_burst: int = 2

    # HTTP client behaviour.
    request_timeout_sec: float = 20.0
    retry_attempts: int = 3

    # Optional Postgres storage. Unset -> in-memory cache (stdio-friendly default).
    database_url: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        def _time(name: str, default: time) -> time:
            raw = os.environ.get(name)
            if not raw:
                return default
            hh, mm = raw.split(":")
            return time(int(hh), int(mm))

        return cls(
            base_url=os.environ.get("PSE_EDGE_BASE_URL", cls.base_url),
            market_open=_time("PSE_MARKET_OPEN", cls.market_open),
            market_close=_time("PSE_MARKET_CLOSE", cls.market_close),
            throttle_rate_per_sec=float(
                os.environ.get("PSE_THROTTLE_RPS", cls.throttle_rate_per_sec)
            ),
            throttle_burst=int(os.environ.get("PSE_THROTTLE_BURST", cls.throttle_burst)),
            request_timeout_sec=float(os.environ.get("PSE_TIMEOUT_SEC", cls.request_timeout_sec)),
            retry_attempts=int(os.environ.get("PSE_RETRY_ATTEMPTS", cls.retry_attempts)),
            database_url=os.environ.get("DATABASE_URL"),
        )
