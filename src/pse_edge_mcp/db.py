"""Postgres schema and engine factory.

Three tables, two jobs:

- `cache_entries` is the freeze-policy cache made shared and durable. It stores the same
  (value, fetched_at) pair `InMemoryStorage` holds in a dict, so **expiry is still decided
  by the calendar at read time** (plan §5a) — nothing here has a TTL, and no row is ever
  evicted on a clock. Two processes behind one database therefore agree on what is fresh.
- `eod_bars` and `disclosures` are the **opportunistic archive** (plan §6a): rows accrue
  only from fetches a user already triggered, so the archive deepens over time at zero
  extra cost to PSE Edge. Edge itself serves limited history, which is the whole point.

Schema changes go through Alembic (`migrations/`), never `create_all` at runtime — a
server that mutates its own schema on boot is impossible to reason about across replicas.
The one exception is tests, which build a throwaway database.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

metadata = MetaData()

cache_entries = Table(
    "cache_entries",
    metadata,
    Column("key", String, primary_key=True),
    # JSONB, not text: cached values are HTML strings, dicts and lists alike, and JSONB
    # accepts any JSON value while keeping the column queryable if that ever helps.
    Column("value", JSONB, nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
)

eod_bars = Table(
    "eod_bars",
    metadata,
    # Keyed by (company_id, security_id, trade_date): a company can list several
    # securities, and each has its own daily bar.
    Column("company_id", String, primary_key=True),
    Column("security_id", String, primary_key=True),
    Column("trade_date", Date, primary_key=True),
    Column("symbol", String),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("value", Float),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

disclosures = Table(
    "disclosures",
    metadata,
    # edge_no is PSE Edge's own stable natural key, and a published disclosure never
    # changes — so this is an append-only record of everything we have ever seen.
    Column("edge_no", String, primary_key=True),
    Column("company_id", String, index=True),
    Column("company_name", String),
    Column("template", String),
    Column("announced_at", DateTime(timezone=True), index=True),
    Column("pse_form_number", String),
    Column("circular_number", String),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def create_engine(database_url: str, *, pool_size: int = 5, max_overflow: int = 10) -> AsyncEngine:
    """Build the async engine.

    `pool_pre_ping` because this process may idle for hours between market boundaries —
    long enough for the database to have closed a pooled connection underneath us, which
    would otherwise surface as a spurious failure on the first query after a quiet spell.

    Pool sizing is configurable (PSE_DB_POOL_SIZE / PSE_DB_MAX_OVERFLOW) because auth
    turns every request into a potential DB lookup: the SQLAlchemy defaults (5+10) are
    fine for the cache/archive alone but become the queue point once bearer validation
    shares the pool.
    """
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        future=True,
    )


def normalise_url(database_url: str) -> str:
    """Accept a plain `postgresql://` URL and route it to the async driver.

    Operators and platform providers hand out `postgresql://…`; SQLAlchemy needs the
    driver named explicitly for async use. Rejecting the common form over a missing
    `+asyncpg` would be a pointless papercut.
    """
    if database_url.startswith("postgresql+"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):  # Heroku-style
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


# --- Phase 5 stage 1: accounts and bearer tokens -----------------------------
#
# `users` holds the account plus its quota overrides; `auth_tokens` holds SHA-256 hashes
# of opaque bearer tokens — the plaintext is shown once at issue time and never stored,
# so a database leak does not leak usable credentials. There is deliberately no password
# column anywhere: registration is passkey-based (plan §6), and until that ships accounts
# are provisioned by the admin CLI.

users = Table(
    "users",
    metadata,
    Column("id", String, primary_key=True),  # uuid4 hex, generated in the app
    Column("email", String, nullable=False, unique=True),  # stored lowercased
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("disabled_at", DateTime(timezone=True)),
    # Null means "use the configured default" — per-user overrides are how the admin
    # raises limits for specific users (plan §6) without a tiers table.
    Column("quota_per_minute", Integer),
    Column("quota_per_day", Integer),
)

auth_tokens = Table(
    "auth_tokens",
    metadata,
    Column("token_hash", String, primary_key=True),  # sha256 hex of the bearer token
    Column("user_id", String, ForeignKey("users.id"), nullable=False, index=True),
    Column("kind", String, nullable=False),  # 'access' now; 'refresh' reserved for OAuth
    Column("note", String),  # CLI label, e.g. "laptop personal token"
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("revoked_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
