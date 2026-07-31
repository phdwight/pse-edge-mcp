"""Bearer-token authentication and per-user quotas (Phase 5 stage 1).

Design (plan §6, rationale revised 2026-07-30):

- **Opaque tokens, hashed at rest.** A token is `pse_` + 32 random url-safe bytes; only
  its SHA-256 lands in the database, and the plaintext is shown once at issue time. No
  slow hash: these carry full entropy, so brute-forcing a leaked hash is hopeless anyway
  — bcrypt exists for low-entropy passwords, of which this system has none.
- **The validation cache's TTL is the revocation-latency budget, and nothing else.** A
  revoked or disabled token keeps working for at most `cache_ttl_sec` (default 60 s).
  The maths: `auth lookups/s ≈ min(request rate, active tokens ÷ TTL)` — on an EOD
  service a 5 s cache would save almost nothing, while 60 s cuts auth reads ~6×. Cached
  validity never outlives the token itself, and *negative* results are never cached, so
  a freshly issued token works immediately.
- **Quotas count in-process.** A per-request counter UPDATE is hot-row lock contention —
  the same defect class as the archive-on-cache-hit bug fixed in 0.4.0. Fixed windows
  (minute + UTC day) per user, per replica; with N replicas the effective ceiling is up
  to N× nominal, which is acceptable for abuse prevention (stop the abuser, don't bill
  exactly).

This module must not import SQLAlchemy — it is on the import path of installs without
the `postgres` extra. The Postgres store lives in `auth_store.py`.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

TOKEN_PREFIX = "pse_"

_MINUTE = 60
_DAY = 86_400


def generate_token() -> str:
    """A new bearer token. The prefix makes leaked tokens greppable by secret scanners."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


@dataclass(frozen=True)
class TokenRecord:
    """What a store returns for a token hash — the raw facts, judged by TokenService."""

    user_id: str
    email: str
    expires_at: datetime
    revoked_at: datetime | None
    user_disabled_at: datetime | None
    quota_per_minute: int | None  # null = use the configured default
    quota_per_day: int | None


@dataclass(frozen=True)
class AuthContext:
    """An authenticated caller, with quota limits already resolved."""

    user_id: str
    email: str
    quota_per_minute: int
    quota_per_day: int


class AuthStore(Protocol):
    async def lookup(self, token_hash: str) -> TokenRecord | None: ...


class TokenService:
    """Validates bearer tokens against a store, with the revocation-budget cache."""

    def __init__(
        self,
        store: AuthStore,
        *,
        cache_ttl_sec: float = 60.0,
        default_quota_per_minute: int = 60,
        default_quota_per_day: int = 2_000,
        max_cache_entries: int = 100_000,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._cache_ttl = cache_ttl_sec
        self._default_qpm = default_quota_per_minute
        self._default_qpd = default_quota_per_day
        self._max_entries = max_cache_entries
        self._now = now or (lambda: datetime.now(UTC))
        self._cache: OrderedDict[str, tuple[AuthContext, datetime]] = OrderedDict()

    async def authenticate(self, token: str) -> AuthContext | None:
        """Plaintext bearer token -> AuthContext, or None for anything not valid.

        One indexed lookup on miss; a hit costs a dict read. Returns None rather than
        raising so the middleware chooses the HTTP shape of the refusal.
        """
        token_hash = hash_token(token)
        now = self._now()

        cached = self._cache.get(token_hash)
        if cached is not None and now < cached[1]:
            self._cache.move_to_end(token_hash)
            return cached[0]

        record = await self._store.lookup(token_hash)
        if (
            record is None
            or record.revoked_at is not None
            or record.user_disabled_at is not None
            or record.expires_at <= now
        ):
            # Never cache a refusal: a just-issued token must work immediately, and an
            # attacker probing random tokens gets a DB lookup each time, which the quota
            # layer above and any edge rate limit are the right tools against.
            self._cache.pop(token_hash, None)
            return None

        context = AuthContext(
            user_id=record.user_id,
            email=record.email,
            quota_per_minute=record.quota_per_minute or self._default_qpm,
            quota_per_day=record.quota_per_day or self._default_qpd,
        )
        expiry_capped = min(now.timestamp() + self._cache_ttl, record.expires_at.timestamp())
        self._cache[token_hash] = (
            context,
            datetime.fromtimestamp(expiry_capped, tz=UTC),
        )
        self._cache.move_to_end(token_hash)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return context


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    retry_after_seconds: int = 0


class QuotaTracker:
    """Fixed-window request counting, minute + UTC day, in process memory.

    Windows are epoch-aligned (`epoch // 60`, `epoch // 86400`), so every replica agrees
    on window boundaries without coordination. The user table is LRU-bounded because the
    user population is unbounded but the *active* population is not.
    """

    def __init__(
        self, *, max_users: int = 100_000, time_fn: Callable[[], float] | None = None
    ) -> None:
        self._time = time_fn or time.time
        self._max_users = max_users
        # user_id -> [minute_window, minute_count, day_window, day_count]
        self._windows: OrderedDict[str, list[int]] = OrderedDict()

    def check(self, context: AuthContext) -> QuotaDecision:
        """Count this request against the caller's limits; deny with a retry hint."""
        now = int(self._time())
        minute, day = now // _MINUTE, now // _DAY

        state = self._windows.get(context.user_id)
        if state is None:
            state = [minute, 0, day, 0]
            self._windows[context.user_id] = state
            while len(self._windows) > self._max_users:
                self._windows.popitem(last=False)
        self._windows.move_to_end(context.user_id)

        if state[0] != minute:
            state[0], state[1] = minute, 0
        if state[2] != day:
            state[2], state[3] = day, 0

        if state[3] >= context.quota_per_day:
            return QuotaDecision(False, retry_after_seconds=(day + 1) * _DAY - now)
        if state[1] >= context.quota_per_minute:
            return QuotaDecision(False, retry_after_seconds=(minute + 1) * _MINUTE - now)

        state[1] += 1
        state[3] += 1
        return QuotaDecision(True)
