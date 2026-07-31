"""Privacy compliance: usage accounting, retention, and account erasure (plan §6a).

The erasure tests are written to catch the failure that matters — data surviving a
deletion the privacy page promised was complete. `test_erasure_leaves_nothing_behind`
walks the schema rather than a hand-written list, so a table added later that references
a user fails this test instead of silently retaining personal data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from pse_edge_mcp.accounts import AccountError, erase, purge_usage, summarise
from pse_edge_mcp.admin import create_user, issue_token
from pse_edge_mcp.db import metadata, usage_events
from pse_edge_mcp.usage import NullUsageRecorder, UsageBucket, UsageRecorder
from pse_edge_mcp.usage_postgres import PostgresUsageSink

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 7, 31, 14, 30, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


# --- the recorder ------------------------------------------------------------


async def test_counts_aggregate_per_user_hour_rather_than_per_request(pg_engine):
    """One row per user-hour, not per request — holding less is the compliance goal."""
    await create_user(pg_engine, "counts@example.com")
    async with pg_engine.connect() as conn:
        user_id = (
            await conn.execute(text("SELECT id FROM users WHERE email = 'counts@example.com'"))
        ).scalar_one()

    recorder = UsageRecorder(PostgresUsageSink(pg_engine), now=Clock())
    for _ in range(500):
        recorder.record(user_id)
    for _ in range(3):
        recorder.record(user_id, rejected=True)
    await recorder.flush()

    async with pg_engine.connect() as conn:
        rows = (await conn.execute(select(usage_events))).all()
    assert len(rows) == 1, "503 requests must collapse into a single user-hour row"
    assert rows[0].requests == 503
    assert rows[0].rejected == 3
    assert rows[0].hour == 14


async def test_flushes_accumulate_rather_than_overwrite(pg_engine):
    """Replicas flush the same bucket independently; a write would erase their peers'
    counts, so the upsert adds."""
    await create_user(pg_engine, "accumulate@example.com")
    async with pg_engine.connect() as conn:
        user_id = (
            await conn.execute(text("SELECT id FROM users WHERE email = 'accumulate@example.com'"))
        ).scalar_one()

    sink = PostgresUsageSink(pg_engine)
    bucket = UsageBucket(user_id=user_id, day=date(2026, 7, 31), hour=14)
    await sink.persist({bucket: (10, 1)})
    await sink.persist({bucket: (5, 2)})  # a second replica's flush

    async with pg_engine.connect() as conn:
        row = (await conn.execute(select(usage_events))).first()
    assert (row.requests, row.rejected) == (15, 3)


async def test_a_failing_sink_never_raises_into_the_request_path(pg_engine):
    """Usage accounting rides along on real requests; losing it must not break them."""

    class BrokenSink:
        async def persist(self, counts):
            raise ConnectionRefusedError("database is down")

        async def purge_older_than(self, cutoff):
            raise ConnectionRefusedError("database is down")

    recorder = UsageRecorder(BrokenSink(), now=Clock())
    recorder.record("u1")
    await recorder.flush()  # must not raise


async def test_buffer_is_cleared_even_when_the_sink_fails(pg_engine):
    """Otherwise a persistent outage grows the buffer without bound."""

    class BrokenSink:
        async def persist(self, counts):
            raise RuntimeError("nope")

        async def purge_older_than(self, cutoff):
            return 0

    recorder = UsageRecorder(BrokenSink(), now=Clock())
    recorder.record("u1")
    await recorder.flush()
    assert recorder._counts == {}


async def test_null_recorder_is_inert():
    recorder = NullUsageRecorder()
    recorder.record("u1", rejected=True)
    await recorder.flush()


# --- retention ---------------------------------------------------------------


async def test_retention_purges_only_rows_past_the_window(pg_engine):
    await create_user(pg_engine, "retention@example.com")
    async with pg_engine.connect() as conn:
        user_id = (
            await conn.execute(text("SELECT id FROM users WHERE email = 'retention@example.com'"))
        ).scalar_one()

    sink = PostgresUsageSink(pg_engine)
    today = date(2026, 7, 31)
    for age in (0, 45, 89, 90, 200):
        await sink.persist({UsageBucket(user_id, today - timedelta(days=age), 1): (1, 0)})

    removed = await purge_usage(pg_engine, today - timedelta(days=90))

    assert removed == 1, "only the 200-day-old row is past a 90-day window"
    async with pg_engine.connect() as conn:
        remaining = (
            await conn.execute(select(func.count()).select_from(usage_events))
        ).scalar_one()
    assert remaining == 4


async def test_the_recorder_purges_at_most_once_a_day(pg_engine):
    """The purge rides on flushes, so it must not run on every one of them."""
    purges = []

    class CountingSink:
        async def persist(self, counts):
            return None

        async def purge_older_than(self, cutoff):
            purges.append(cutoff)
            return 0

    clock = Clock()
    recorder = UsageRecorder(CountingSink(), retention_days=90, now=clock)
    await recorder.flush()
    await recorder.flush()
    await recorder.flush()
    assert len(purges) == 1, "three flushes in one day means one purge"
    assert purges[0] == T0.date() - timedelta(days=90)

    clock.now = T0 + timedelta(days=1)
    await recorder.flush()
    assert len(purges) == 2, "a new day purges again"


# --- subject access ----------------------------------------------------------


async def test_summary_shows_what_is_held_without_exposing_secrets(pg_engine):
    user_id = await create_user(pg_engine, "subject@example.com")
    await issue_token(pg_engine, "subject@example.com")
    await PostgresUsageSink(pg_engine).persist(
        {UsageBucket(user_id, date(2026, 7, 31), 9): (12, 1)}
    )

    summary = await summarise(pg_engine, user_id)

    assert summary.email == "subject@example.com"
    assert summary.active_tokens == 1
    assert summary.passkeys == 0
    assert summary.usage_days[0]["requests"] == 12
    # The token itself must not be recoverable from the subject-access view.
    assert "token" not in str(summary).lower().replace("active_tokens", "")


# --- erasure -----------------------------------------------------------------


async def test_erasure_leaves_nothing_behind(pg_engine):
    """The promise on the privacy page, enforced against the live schema.

    Rather than checking a hand-written list of tables, this walks every table that has a
    `user_id` column — so a table added later that references a user fails here instead of
    quietly retaining personal data after a deletion.
    """
    user_id = await create_user(pg_engine, "erase-me@example.com")
    await issue_token(pg_engine, "erase-me@example.com")
    await PostgresUsageSink(pg_engine).persist({UsageBucket(user_id, date(2026, 7, 31), 9): (5, 0)})
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webauthn_credentials (credential_id, user_id, public_key, "
                "sign_count) VALUES ('cred-1', :uid, 'pk', 0)"
            ),
            {"uid": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO web_sessions (sid_hash, user_id, email, kind, expires_at) "
                "VALUES ('sid-1', :uid, 'erase-me@example.com', 'authenticated', now())"
            ),
            {"uid": user_id},
        )

    removed = await erase(pg_engine, user_id)
    assert removed["users"] == 1

    user_tables = [
        table for table in metadata.tables.values() if "user_id" in table.c or table.name == "users"
    ]
    assert len(user_tables) >= 6, "sanity: the walk should cover the user-keyed tables"

    async with pg_engine.connect() as conn:
        for table in user_tables:
            column = table.c.id if table.name == "users" else table.c.user_id
            count = (
                await conn.execute(select(func.count()).select_from(table).where(column == user_id))
            ).scalar_one()
            assert count == 0, f"{table.name} still holds rows for the erased user"


async def test_erasure_keeps_public_market_data(pg_engine):
    """Bars and disclosures are public PSE facts, never personal data — deleting an
    account must not erase the archive."""
    user_id = await create_user(pg_engine, "keep-market@example.com")
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eod_bars (company_id, security_id, trade_date, close) "
                "VALUES ('599', '520', DATE '2026-07-29', 599.5)"
            )
        )
        await conn.execute(
            text("INSERT INTO disclosures (edge_no, company_id) VALUES ('a1', '599')")
        )

    await erase(pg_engine, user_id)

    async with pg_engine.connect() as conn:
        bars = (await conn.execute(text("SELECT count(*) FROM eod_bars"))).scalar_one()
        disc = (await conn.execute(text("SELECT count(*) FROM disclosures"))).scalar_one()
    assert (bars, disc) == (1, 1)


async def test_erasing_an_unknown_account_is_refused(pg_engine):
    with pytest.raises(AccountError, match="not found"):
        await erase(pg_engine, "no-such-user")


async def test_erased_tokens_stop_authenticating(pg_engine):
    """The end a user actually cares about: after deletion their credentials are dead."""
    from pse_edge_mcp.auth import TokenService
    from pse_edge_mcp.auth_store import PostgresAuthStore

    user_id = await create_user(pg_engine, "dead-token@example.com")
    token = await issue_token(pg_engine, "dead-token@example.com")
    service = TokenService(PostgresAuthStore(pg_engine), cache_ttl_sec=0.0)
    assert await service.authenticate(token) is not None

    await erase(pg_engine, user_id)

    assert await service.authenticate(token) is None
