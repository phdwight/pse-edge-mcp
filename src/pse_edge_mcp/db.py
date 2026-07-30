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


def create_engine(database_url: str) -> AsyncEngine:
    """Build the async engine.

    `pool_pre_ping` because this process may idle for hours between market boundaries —
    long enough for the database to have closed a pooled connection underneath us, which
    would otherwise surface as a spurious failure on the first query after a quiet spell.
    """
    return create_async_engine(database_url, pool_pre_ping=True, future=True)


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
