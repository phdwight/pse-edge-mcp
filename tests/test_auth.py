"""TokenService and QuotaTracker, driven entirely by fakes and injected clocks.

The behaviours pinned here are the plan §6 decisions (rationale revised 2026-07-30):
the cache TTL is the revocation-latency budget and nothing else; refusals are never
cached; cached validity never outlives the token; quotas count in fixed windows with
per-user overrides.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pse_edge_mcp.auth import (
    TOKEN_PREFIX,
    AuthContext,
    QuotaTracker,
    TokenRecord,
    TokenService,
    generate_token,
    hash_token,
)

T0 = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeStore:
    def __init__(self) -> None:
        self.records: dict[str, TokenRecord] = {}
        self.lookups = 0

    async def lookup(self, token_hash: str) -> TokenRecord | None:
        self.lookups += 1
        return self.records.get(token_hash)

    def add(self, token: str, **overrides) -> TokenRecord:
        record = TokenRecord(
            **{
                "user_id": "u1",
                "email": "user@example.com",
                "expires_at": T0 + timedelta(minutes=30),
                "revoked_at": None,
                "user_disabled_at": None,
                "quota_per_minute": None,
                "quota_per_day": None,
                **overrides,
            }
        )
        self.records[hash_token(token)] = record
        return record


def make_service(store: FakeStore, clock: Clock, **kwargs) -> TokenService:
    return TokenService(store, cache_ttl_sec=60.0, now=clock, **kwargs)


# --- token material ----------------------------------------------------------


def test_tokens_are_prefixed_unique_and_never_stored_in_plaintext():
    a, b = generate_token(), generate_token()
    assert a.startswith(TOKEN_PREFIX) and b.startswith(TOKEN_PREFIX)
    assert a != b
    assert len(a) > 40
    assert hash_token(a) != a
    assert hash_token(a) == hash_token(a)  # deterministic: the DB key is reproducible


# --- authentication ----------------------------------------------------------


async def test_valid_token_authenticates_with_default_quotas():
    store, clock = FakeStore(), Clock()
    token = generate_token()
    store.add(token)

    ctx = await make_service(store, clock).authenticate(token)

    assert ctx is not None
    assert ctx.user_id == "u1"
    assert (ctx.quota_per_minute, ctx.quota_per_day) == (60, 2000)


async def test_per_user_overrides_beat_the_defaults():
    store, clock = FakeStore(), Clock()
    token = generate_token()
    store.add(token, quota_per_minute=600, quota_per_day=50_000)

    ctx = await make_service(store, clock).authenticate(token)

    assert ctx is not None
    assert (ctx.quota_per_minute, ctx.quota_per_day) == (600, 50_000)


async def test_revoked_expired_and_disabled_are_all_refused():
    store, clock = FakeStore(), Clock()
    service = make_service(store, clock)
    for name, overrides in [
        ("revoked", {"revoked_at": T0 - timedelta(minutes=1)}),
        ("expired", {"expires_at": T0 - timedelta(seconds=1)}),
        ("disabled", {"user_disabled_at": T0 - timedelta(days=1)}),
    ]:
        token = generate_token()
        store.add(token, **overrides)
        assert await service.authenticate(token) is None, name


async def test_unknown_token_is_never_negative_cached():
    """A just-issued token must work immediately, so refusals hit the store each time."""
    store, clock = FakeStore(), Clock()
    service = make_service(store, clock)
    token = generate_token()

    assert await service.authenticate(token) is None
    assert await service.authenticate(token) is None
    assert store.lookups == 2

    store.add(token)  # "issued" between requests
    assert await service.authenticate(token) is not None


async def test_cache_ttl_is_the_revocation_latency_budget():
    """Within the TTL a revoked token still works (the accepted budget); one tick past
    it, the store is consulted again and the revocation lands."""
    store, clock = FakeStore(), Clock()
    service = make_service(store, clock)
    token = generate_token()
    store.add(token)

    assert await service.authenticate(token) is not None
    assert store.lookups == 1

    # Revoke in the store; the cache hides it for at most 60 s.
    store.records[hash_token(token)] = TokenRecord(
        user_id="u1",
        email="user@example.com",
        expires_at=T0 + timedelta(minutes=30),
        revoked_at=clock.now,
        user_disabled_at=None,
        quota_per_minute=None,
        quota_per_day=None,
    )
    clock.advance(59)
    assert await service.authenticate(token) is not None, "inside the budget: still cached"
    assert store.lookups == 1

    clock.advance(2)  # past the 60 s budget
    assert await service.authenticate(token) is None
    assert store.lookups == 2


async def test_cached_validity_never_outlives_the_token():
    """A token expiring in 10 s must not ride a 60 s cache entry past its own expiry."""
    store, clock = FakeStore(), Clock()
    service = make_service(store, clock)
    token = generate_token()
    store.add(token, expires_at=T0 + timedelta(seconds=10))

    assert await service.authenticate(token) is not None
    clock.advance(11)
    assert await service.authenticate(token) is None


async def test_cache_is_lru_bounded():
    store, clock = FakeStore(), Clock()
    service = TokenService(store, cache_ttl_sec=60.0, max_cache_entries=2, now=clock)
    tokens = [generate_token() for _ in range(3)]
    for token in tokens:
        store.add(token)
        await service.authenticate(token)

    lookups_before = store.lookups
    await service.authenticate(tokens[2])  # newest: cached
    assert store.lookups == lookups_before
    await service.authenticate(tokens[0])  # oldest: evicted, needs the store again
    assert store.lookups == lookups_before + 1


# --- quotas ------------------------------------------------------------------


def ctx(user_id: str = "u1", per_minute: int = 3, per_day: int = 10) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        email=f"{user_id}@example.com",
        quota_per_minute=per_minute,
        quota_per_day=per_day,
    )


def test_requests_within_the_limit_are_allowed():
    fake_time = [1_000_000.0]
    quotas = QuotaTracker(time_fn=lambda: fake_time[0])
    for _ in range(3):
        assert quotas.check(ctx()).allowed


def test_minute_limit_denies_with_a_retry_hint_and_resets_next_window():
    fake_time = [1_000_000.0]  # 1_000_000 % 60 == 40 → 20 s left in this minute
    quotas = QuotaTracker(time_fn=lambda: fake_time[0])
    for _ in range(3):
        assert quotas.check(ctx()).allowed

    denied = quotas.check(ctx())
    assert not denied.allowed
    assert 0 < denied.retry_after_seconds <= 60
    assert denied.retry_after_seconds == 20  # exact: seconds to the next minute boundary

    fake_time[0] += denied.retry_after_seconds
    assert quotas.check(ctx()).allowed, "the next window starts clean"


def test_day_limit_outranks_the_minute_window():
    fake_time = [1_000_000.0]
    quotas = QuotaTracker(time_fn=lambda: fake_time[0])
    generous = ctx(per_minute=1000, per_day=5)
    for _ in range(5):
        assert quotas.check(generous).allowed

    denied = quotas.check(generous)
    assert not denied.allowed
    assert denied.retry_after_seconds > 60, "day-window retry, not minute"


def test_users_do_not_share_windows():
    fake_time = [1_000_000.0]
    quotas = QuotaTracker(time_fn=lambda: fake_time[0])
    for _ in range(3):
        assert quotas.check(ctx("a")).allowed
    assert not quotas.check(ctx("a")).allowed
    assert quotas.check(ctx("b")).allowed, "user b has their own window"


def test_quota_state_is_lru_bounded():
    fake_time = [1_000_000.0]
    quotas = QuotaTracker(max_users=2, time_fn=lambda: fake_time[0])
    assert quotas.check(ctx("a", per_minute=1)).allowed
    assert quotas.check(ctx("b", per_minute=1)).allowed
    assert quotas.check(ctx("c", per_minute=1)).allowed  # evicts a
    # a's window was forgotten, so a gets a fresh allowance rather than a denial.
    assert quotas.check(ctx("a", per_minute=1)).allowed
