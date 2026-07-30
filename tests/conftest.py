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
