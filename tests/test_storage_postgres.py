"""Postgres storage and archive, against a real ephemeral Postgres 18 (plan §4).

These are the only tests that need Docker. They skip cleanly when it is unavailable so a
laptop without a running daemon can still run the suite; CI has Docker and runs them.

The schema is built by applying the **real Alembic migration**, not `metadata.create_all`.
That way the migration itself is under test — a migration that drifts from `db.py` is
exactly the kind of breakage that otherwise surfaces first in production.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from pse_edge_mcp.archive import NullArchive
from pse_edge_mcp.cache import CacheEntry
from pse_edge_mcp.db import cache_entries, disclosures, eod_bars, normalise_url
from pse_edge_mcp.models import DisclosureHit, OhlcBar

MNL = ZoneInfo("Asia/Manila")

pytestmark = pytest.mark.postgres


@pytest.fixture
async def engine(pg_engine):
    """Alias onto the shared conftest engine; this file predates the shared name."""
    return pg_engine


# --- migration ---------------------------------------------------------------


async def test_migration_creates_the_schema_db_py_declares(engine):
    """The migration and `db.py` must agree; a silent divergence would only show up when a
    query hits a column the migration never created."""
    from pse_edge_mcp.db import metadata

    async with engine.connect() as conn:
        assert len(metadata.tables) >= 5  # cache, bars, disclosures, users, auth_tokens
        for table in metadata.tables.values():
            result = await conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table.name},
            )
            actual = {row.column_name for row in result}
            declared = {c.name for c in table.columns}
            assert declared == actual, f"{table.name}: declared {declared}, migrated {actual}"


async def test_check_schema_rejects_an_unmigrated_database(postgres_url, alembic_migrate):
    """A missing schema must fail at startup with an actionable message, not mid-request
    with an opaque UndefinedTableError."""
    from pse_edge_mcp.db import create_engine
    from pse_edge_mcp.storage_postgres import check_schema

    await alembic_migrate(postgres_url, "down", "base")
    eng = create_engine(postgres_url)
    try:
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            await check_schema(eng)
    finally:
        await eng.dispose()


# --- storage -----------------------------------------------------------------


async def test_storage_roundtrips_every_shape_a_cached_value_takes(engine):
    """Cached values are HTML strings, JSON dicts and JSON lists — all must survive."""
    from pse_edge_mcp.storage_postgres import PostgresStorage

    storage = PostgresStorage(engine)
    fetched = datetime(2026, 7, 30, 16, 30, tzinfo=MNL)

    for key, value in [
        ("html", "<table class='list'>rows</table>"),
        ("dict", {"chartData": [{"OPEN": 1.5}], "tableData": []}),
        ("list", [{"cmpyId": "599", "symbol": "SM"}]),
    ]:
        await storage.set(key, CacheEntry(value=value, fetched_at=fetched))
        entry = await storage.get(key)
        assert entry is not None
        assert entry.value == value, key
        assert entry.fetched_at == fetched


async def test_storage_returns_none_for_an_unknown_key(engine):
    from pse_edge_mcp.storage_postgres import PostgresStorage

    assert await PostgresStorage(engine).get("never-written") is None


async def test_storage_overwrites_rather_than_conflicting(engine):
    """Replicas can both miss and both write the same key; the second must not raise.

    Single-flight only collapses concurrent misses within one process, so across processes
    a race is expected. Both are fetching the same frozen EOD value, so last-write-wins is
    harmless — an INSERT would have failed instead.
    """
    from pse_edge_mcp.storage_postgres import PostgresStorage

    storage = PostgresStorage(engine)
    first = datetime(2026, 7, 30, 16, 0, tzinfo=MNL)
    second = datetime(2026, 7, 31, 16, 0, tzinfo=MNL)

    await storage.set("k", CacheEntry(value={"v": 1}, fetched_at=first))
    await storage.set("k", CacheEntry(value={"v": 2}, fetched_at=second))

    entry = await storage.get("k")
    assert entry is not None
    assert entry.value == {"v": 2}
    assert entry.fetched_at == second


async def test_storage_has_no_ttl_column_so_the_calendar_stays_in_charge(engine):
    """Freshness is the market calendar's decision (plan §5a). A TTL or expiry column here
    would quietly introduce a second, competing policy."""
    columns = {c.name for c in cache_entries.columns}
    assert columns == {"key", "value", "fetched_at"}
    assert not any("expire" in c or "ttl" in c for c in columns)


# --- archive -----------------------------------------------------------------


async def test_archive_records_bars_and_ignores_repeat_sightings(engine):
    """A closed bar is immutable, so re-fetching an overlapping range must not churn rows
    or move first_seen_at."""
    from pse_edge_mcp.archive_postgres import PostgresArchive

    archive = PostgresArchive(engine)
    bars = [
        OhlcBar(trade_date=date(2026, 7, 28), open=840, high=848, low=838, close=845.5, value=1),
        OhlcBar(trade_date=date(2026, 7, 29), open=846, high=851, low=840, close=847, value=2),
    ]
    await archive.record_bars(company_id="599", security_id="520", symbol="SM", bars=bars)

    async with engine.connect() as conn:
        rows = (await conn.execute(select(eod_bars))).all()
    assert len(rows) == 2
    first_seen = {r.trade_date: r.first_seen_at for r in rows}

    # Same range again, plus one new day.
    bars.append(
        OhlcBar(trade_date=date(2026, 7, 30), open=847, high=850, low=845, close=849, value=3)
    )
    await archive.record_bars(company_id="599", security_id="520", symbol="SM", bars=bars)

    async with engine.connect() as conn:
        rows = (await conn.execute(select(eod_bars))).all()
    assert len(rows) == 3, "re-fetch must add only the new bar"
    for row in rows:
        if row.trade_date in first_seen:
            assert row.first_seen_at == first_seen[row.trade_date], "first sighting preserved"


async def test_archive_separates_securities_of_the_same_company(engine):
    """A company can list several securities, each with its own daily bar — they must not
    collide on (company_id, trade_date)."""
    from pse_edge_mcp.archive_postgres import PostgresArchive

    archive = PostgresArchive(engine)
    bar = [OhlcBar(trade_date=date(2026, 7, 29), open=1, high=2, low=1, close=2, value=1)]
    await archive.record_bars(company_id="599", security_id="520", symbol="SM", bars=bar)
    await archive.record_bars(company_id="599", security_id="999", symbol="SMP", bars=bar)

    async with engine.connect() as conn:
        rows = (await conn.execute(select(eod_bars))).all()
    assert len(rows) == 2


async def test_archive_records_disclosures_and_dedupes_within_a_batch(engine):
    """One page can repeat an edge_no, and Postgres rejects a statement touching the same
    key twice — so the batch is deduplicated before it is sent."""
    from pse_edge_mcp.archive_postgres import PostgresArchive

    hit = DisclosureHit(
        edge_no="a" * 32,
        template="Share Buy-Back Transactions",
        company_name="SM Investments Corporation",
        company_id="599",
        announced_at=datetime(2026, 7, 29, 8, 11, tzinfo=MNL),
        pse_form_number="9-1",
        circular_number="C05694-2026",
    )
    duplicate = hit.model_copy()
    other = hit.model_copy(update={"edge_no": "b" * 32, "circular_number": "C05695-2026"})

    await PostgresArchive(engine).record_disclosures([hit, duplicate, other])

    async with engine.connect() as conn:
        rows = (await conn.execute(select(disclosures))).all()
    assert len(rows) == 2
    stored = {r.edge_no: r for r in rows}
    assert stored["a" * 32].company_id == "599"
    assert stored["a" * 32].template == "Share Buy-Back Transactions"
    assert stored["a" * 32].announced_at == hit.announced_at


async def test_archive_skips_hits_without_an_edge_no(engine):
    from pse_edge_mcp.archive_postgres import PostgresArchive

    await PostgresArchive(engine).record_disclosures(
        [DisclosureHit(edge_no="c" * 32), DisclosureHit(edge_no="")]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(disclosures))).all()
    assert len(rows) == 1


async def test_archive_write_failure_does_not_propagate(engine):
    """Bookkeeping must never break the read that carried it: a broken engine is logged
    and swallowed, so the caller still gets their data."""
    from pse_edge_mcp.archive_postgres import PostgresArchive
    from pse_edge_mcp.db import create_engine

    dead = create_engine("postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing")
    try:
        await PostgresArchive(dead).record_disclosures([DisclosureHit(edge_no="d" * 32)])
    finally:
        await dead.dispose()


# --- no-Docker checks --------------------------------------------------------


async def test_null_archive_accepts_everything_and_does_nothing():
    """The stdio default. Present so the protocol's no-op path is covered without Docker."""
    archive = NullArchive()
    await archive.record_bars(company_id="1", security_id="2", symbol="X", bars=[])
    await archive.record_disclosures([DisclosureHit(edge_no="e" * 32)])


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ],
)
def test_url_normalisation_accepts_the_forms_operators_actually_paste(given, expected):
    """Platform providers hand out `postgresql://` and Heroku-style `postgres://`; failing
    on a missing `+asyncpg` would be a pointless papercut."""
    assert normalise_url(given) == expected


# --- end-to-end wiring -------------------------------------------------------


async def test_disclosure_search_through_the_tool_populates_the_archive(
    postgres_url, alembic_migrate, announcements_html
):
    """The archive must fill from ordinary use, with no crawler and no extra upstream call
    (plan §6a). Driven through the MCP tool so the whole wiring is under test.
    """
    import httpx
    import respx

    from pse_edge_mcp.archive_postgres import PostgresArchive
    from pse_edge_mcp.config import Settings
    from pse_edge_mcp.db import create_engine
    from pse_edge_mcp.market_calendar import MarketCalendar
    from pse_edge_mcp.server import build_server
    from pse_edge_mcp.storage_postgres import PostgresStorage

    await alembic_migrate(postgres_url, "up", "head")
    engine = create_engine(postgres_url)

    class Closed(MarketCalendar):
        def now(self):
            return datetime(2026, 7, 30, 16, 30, tzinfo=MNL)

    try:
        mcp = build_server(
            Settings(throttle_rate_per_sec=1000),
            calendar=Closed(),
            storage=PostgresStorage(engine),
            archive=PostgresArchive(engine),
        )
        with respx.mock:
            route = respx.post("https://edge.pse.com.ph/announcements/search.ax").mock(
                return_value=httpx.Response(200, text=announcements_html)
            )
            args = {"start_date": "2026-07-01", "end_date": "2026-07-30"}
            first = await mcp.call_tool("search_disclosures", args)
            assert "50" in first.content[0].text or first.content  # sanity: rows returned

            async with engine.connect() as conn:
                archived = (await conn.execute(select(disclosures))).all()
            assert len(archived) == 50, "every hit on the page should be archived"
            assert all(len(r.edge_no) == 32 for r in archived)
            assert any(r.company_name == "Lepanto Consolidated Mining Company" for r in archived)

            # A second identical call is served from the shared cache: no new upstream
            # request, and the archive does not duplicate.
            await mcp.call_tool("search_disclosures", args)
            assert route.call_count == 1
            async with engine.connect() as conn:
                assert len((await conn.execute(select(disclosures))).all()) == 50
    finally:
        await engine.dispose()
        await alembic_migrate(postgres_url, "down", "base")


async def test_cache_is_shared_across_server_instances(
    postgres_url, alembic_migrate, announcements_html
):
    """The reason Postgres exists (plan §5): two processes behind one database must make a
    single upstream fetch per boundary, not one each. With InMemoryStorage this test would
    see two calls.
    """
    import httpx
    import respx

    from pse_edge_mcp.config import Settings
    from pse_edge_mcp.db import create_engine
    from pse_edge_mcp.market_calendar import MarketCalendar
    from pse_edge_mcp.server import build_server
    from pse_edge_mcp.storage_postgres import PostgresStorage

    await alembic_migrate(postgres_url, "up", "head")
    engine = create_engine(postgres_url)

    class Closed(MarketCalendar):
        def now(self):
            return datetime(2026, 7, 30, 16, 30, tzinfo=MNL)

    def make_replica():
        return build_server(
            Settings(throttle_rate_per_sec=1000),
            calendar=Closed(),
            storage=PostgresStorage(engine),
            archive=NullArchive(),
        )

    try:
        with respx.mock:
            route = respx.post("https://edge.pse.com.ph/announcements/search.ax").mock(
                return_value=httpx.Response(200, text=announcements_html)
            )
            args = {"start_date": "2026-07-01", "end_date": "2026-07-30"}
            await make_replica().call_tool("search_disclosures", args)
            result = await make_replica().call_tool("search_disclosures", args)

            assert route.call_count == 1, "second replica must reuse the shared cache"
            import json

            assert json.loads(result.content[0].text)["meta"]["from_cache"] is True
    finally:
        await engine.dispose()
        await alembic_migrate(postgres_url, "down", "base")
