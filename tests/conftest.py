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


# Phase 3 fixtures — recorded live 2026-07-30 (see docs/endpoints.md §4-5).
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
