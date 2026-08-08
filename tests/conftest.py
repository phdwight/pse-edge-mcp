from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stock_data_html() -> str:
    return (FIXTURES / "stock_data.html").read_text()


@pytest.fixture
def autocomplete_json() -> list:
    return json.loads((FIXTURES / "autocomplete.json").read_text())


@pytest.fixture
def chart_json() -> dict:
    return json.loads((FIXTURES / "chart.json").read_text())


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# Disclosure fixtures — all recorded live 2026-07-30 (see docs/endpoints.md).
@pytest.fixture
def announcements_html() -> str:
    """Market-wide announcements, Jul 1-30 2026: full 50-row page 1 of 17."""
    return _fixture("announcements_search.html")


@pytest.fixture
def announcements_short_html() -> str:
    """Single short page (36 rows, no pagination) — Jul 30 2026 only."""
    return _fixture("announcements_short_page.html")


@pytest.fixture
def announcements_empty_html() -> str:
    """Zero results: Edge emits a 'no data.' placeholder row and [Total 0]."""
    return _fixture("announcements_empty.html")


@pytest.fixture
def company_disclosures_html() -> str:
    """SM (cmpy_id 599) full history, page 1 of 7 — note: no Company Name column."""
    return _fixture("company_disclosures_search.html")


@pytest.fixture
def company_disclosures_last_page_html() -> str:
    """SM page 7 of 7: 43 rows (a short last page)."""
    return _fixture("company_disclosures_last_page.html")


@pytest.fixture
def keyword_search_html() -> str:
    """keyword/search.ax for 'dividend' — <dl> of full-text hits with snippets."""
    return _fixture("keyword_search.html")


@pytest.fixture
def disclosure_viewer_html() -> str:
    """openDiscViewer.do for a Lepanto material-information disclosure."""
    return _fixture("disclosure_viewer.html")


# Company-info & market fixtures — recorded live 2026-07-30 (see docs/endpoints.md §4-5).
@pytest.fixture
def company_profile_html() -> str:
    """companyInformation/form.do for SM (cmpy_id 599)."""
    return _fixture("company_profile.html")


@pytest.fixture
def financial_reports_html() -> str:
    """financial_reports_view.do for SM: annual + quarterly, BS + IS each.

    Note the units labels disagree between sections in this real capture.
    """
    return _fixture("financial_reports.html")


@pytest.fixture
def dividends_html() -> str:
    """dividends_and_rights_list.ax?DividendsOrRights=Dividends for SM (one record)."""
    return _fixture("dividends.html")


@pytest.fixture
def rights_empty_html() -> str:
    """The Rights tab for SM: a real 'no data.' response."""
    return _fixture("rights_empty.html")


@pytest.fixture
def homepage_html() -> str:
    """The PSE Edge homepage: Index Summary plus every disclosure feed."""
    return _fixture("homepage.html")


# --- shared Postgres harness (testcontainers) --------------------------------
#
# Session-scoped so every Postgres-marked test file shares one container. The fixture
# skips (rather than fails) when Docker is unavailable, so the suite still runs on a
# machine without a daemon; CI has Docker and runs everything.


def _docker_available() -> bool:
    import os

    if os.environ.get("SKIP_DOCKER_TESTS"):
        return False
    try:
        import docker  # noqa: PLC0415

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """An ephemeral Postgres 18, matching the version compose.yaml pins."""
    if not _docker_available():
        pytest.skip("Docker is not available for testcontainers")
    try:  # the module moved; support both without pinning to a deprecated path
        from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    from pse_edge_mcp.db import normalise_url  # noqa: PLC0415

    with PostgresContainer("postgres:18") as container:
        yield normalise_url(container.get_connection_url().replace("+psycopg2", ""))


@pytest.fixture
def alembic_migrate():
    """Run the real migrations from inside an async test.

    `migrations/env.py` calls `asyncio.run()`, which raises inside pytest-asyncio's
    already-running loop — a worker thread gives it a loop-free home, so production
    env.py needs no test-only branch.
    """
    import asyncio
    import os

    async def run(url: str, direction: str, revision: str) -> None:
        from alembic import command  # noqa: PLC0415
        from alembic.config import Config  # noqa: PLC0415

        os.environ["DATABASE_URL"] = url
        config = Config("alembic.ini")
        runner = command.upgrade if direction == "up" else command.downgrade
        await asyncio.to_thread(runner, config, revision)

    return run


@pytest.fixture
async def pg_engine(postgres_url: str, alembic_migrate):
    """Engine with the schema applied by the real migration, torn down after the test."""
    from pse_edge_mcp.db import create_engine  # noqa: PLC0415

    await alembic_migrate(postgres_url, "up", "head")
    engine = create_engine(postgres_url)
    try:
        yield engine
    finally:
        await engine.dispose()
        await alembic_migrate(postgres_url, "down", "base")
