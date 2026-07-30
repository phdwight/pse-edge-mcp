"""Alembic environment.

The database URL comes from `DATABASE_URL` at runtime, never from alembic.ini — no
connection string, and no credential, is ever committed (invariant #5's no-secrets rule
applies to the repo as much as to image layers).

Migrations run against the async driver through `connection.run_sync`, so the same URL
serves the app and the migration tool.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context

from pse_edge_mcp.db import create_engine, metadata, normalise_url

config = context.config
target_metadata = metadata


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url", "")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. Point it at your Postgres instance, e.g.\n"
            "  DATABASE_URL=postgresql+asyncpg://pse:pse-dev-only@localhost:5432/pse_edge "
            "uv run alembic upgrade head"
        )
    return normalise_url(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
