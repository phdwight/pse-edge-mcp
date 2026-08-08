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
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # Auth. Opt-in (flipping it default-on at deploy is still pending); requires
    # DATABASE_URL, since accounts live in Postgres. stdio mode never authenticates.
    auth_required: bool = False
    # The token-validation cache TTL is the revocation-latency budget and nothing else:
    # a revoked token keeps working for at most this long (plan §6, revised 2026-07-30).
    token_cache_ttl_sec: float = 60.0
    quota_per_minute: int = 60
    quota_per_day: int = 2000

    # The OAuth/passkey surface. public_url is what browsers and OAuth
    # clients see — it drives the WebAuthn rp_id/origin, email links, and the metadata
    # issuer, so it must be the externally reachable base URL in production.
    public_url: str = "http://localhost:8000"
    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 30
    # ZeptoMail (decided 2026-07-30). Key arrives via env only; unset -> emails are
    # logged to the console, which is the dev/test mode.
    zeptomail_api_key: str | None = None
    email_from: str = "no-reply@localhost"
    # Where the schema canary reports failures. Unset means the canary still runs and still
    # logs, but nobody is told — which is worth a warning, because a canary nobody reads is
    # indistinguishable from no canary.
    operator_email: str | None = None
    # Accounts allowed to provision machine clients from the /account web page. This is the
    # SAME authority the `pse-edge-admin create-machine-client` CLI has — moved from "has a
    # shell" to "is a named operator" — so it must stay narrow: the machine-only gate on the
    # client_credentials grant depends on machine clients being created only by an admin, and
    # this list defines who that is over HTTP. Empty (the default) means the web path is off
    # and the CLI is the only route, exactly as before. Never populate it from anything a
    # user can set about themselves.
    admin_emails: frozenset[str] = field(default_factory=frozenset)
    # Privacy (plan §6a): usage counts are deleted after this many days. The privacy page
    # states 90, so changing it here means changing what users were told.
    usage_retention_days: int = 90

    # Transport shape and logging, so the importable ASGI app is configurable without CLI
    # flags (a worker subprocess re-imports the module; it does not re-parse argv).
    stateful_sessions: bool = False
    sse_responses: bool = False
    log_json: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        def _bool(name: str) -> bool:
            return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

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
            db_pool_size=int(os.environ.get("PSE_DB_POOL_SIZE", cls.db_pool_size)),
            db_max_overflow=int(os.environ.get("PSE_DB_MAX_OVERFLOW", cls.db_max_overflow)),
            auth_required=_bool("PSE_AUTH_REQUIRED"),
            token_cache_ttl_sec=float(
                os.environ.get("PSE_TOKEN_CACHE_TTL", cls.token_cache_ttl_sec)
            ),
            quota_per_minute=int(os.environ.get("PSE_QUOTA_PER_MIN", cls.quota_per_minute)),
            quota_per_day=int(os.environ.get("PSE_QUOTA_PER_DAY", cls.quota_per_day)),
            public_url=os.environ.get("PSE_PUBLIC_URL", cls.public_url).rstrip("/"),
            access_token_ttl_min=int(
                os.environ.get("PSE_ACCESS_TTL_MIN", cls.access_token_ttl_min)
            ),
            refresh_token_ttl_days=int(
                os.environ.get("PSE_REFRESH_TTL_DAYS", cls.refresh_token_ttl_days)
            ),
            zeptomail_api_key=os.environ.get("ZEPTOMAIL_API_KEY") or None,
            email_from=os.environ.get("PSE_EMAIL_FROM", cls.email_from),
            operator_email=os.environ.get("PSE_OPERATOR_EMAIL") or None,
            admin_emails=frozenset(
                e.strip().lower()
                for e in os.environ.get("PSE_ADMIN_EMAILS", "").split(",")
                if e.strip()
            ),
            usage_retention_days=int(
                os.environ.get("PSE_USAGE_RETENTION_DAYS", cls.usage_retention_days)
            ),
            stateful_sessions=_bool("PSE_STATEFUL"),
            sse_responses=_bool("PSE_SSE"),
            log_json=_bool("PSE_LOG_JSON"),
            log_level=os.environ.get("PSE_LOG_LEVEL", cls.log_level),
        )
