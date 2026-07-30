"""Argument validators.

These were open-coded in each tool before, which is how the rules drifted apart — one
tool rejected a reversed date range and another silently accepted it. Testing them once,
here, is the point of extracting them.
"""

from __future__ import annotations

from datetime import date

import pytest

from pse_edge_mcp.errors import InvalidArgumentError
from pse_edge_mcp.validation import (
    optional_date,
    require_edge_no,
    require_ordered,
    require_page,
    require_text,
    resolve_window,
)

EDGE_NO = "ff4c7557aee1d72b64d70b69f0a3140b"


@pytest.mark.parametrize("page", [1, 2, 500])
def test_valid_pages_pass_through(page):
    assert require_page(page) == page


@pytest.mark.parametrize("page", [0, -1])
def test_non_positive_pages_are_rejected(page):
    with pytest.raises(InvalidArgumentError, match="page must be 1 or greater"):
        require_page(page)


def test_require_text_strips_and_rejects_blank():
    assert require_text("  SM  ", "symbol") == "SM"
    with pytest.raises(InvalidArgumentError, match="symbol must not be empty"):
        require_text("   ", "symbol")


def test_optional_date_accepts_none_and_iso():
    assert optional_date(None, "start_date") is None
    assert optional_date("2026-07-30", "start_date") == date(2026, 7, 30)


@pytest.mark.parametrize("bad", ["30-07-2026", "07/30/2026", "2026-13-01", "yesterday"])
def test_malformed_dates_become_invalid_argument_not_internal_error(bad):
    """date.fromisoformat raises ValueError; unmapped that would surface as an opaque
    INTERNAL_ERROR instead of telling the caller which argument was wrong."""
    with pytest.raises(InvalidArgumentError, match="start_date"):
        optional_date(bad, "start_date")


def test_reversed_range_is_rejected_but_equal_dates_are_fine():
    require_ordered(date(2026, 7, 1), date(2026, 7, 1))  # single-day window
    require_ordered(None, date(2026, 7, 1))  # partial ranges are the caller's business
    with pytest.raises(InvalidArgumentError, match="is after"):
        require_ordered(date(2026, 7, 30), date(2026, 7, 1))


def test_resolve_window_defaults_backwards_from_today():
    start, end = resolve_window(None, None, default_days=30, today=date(2026, 7, 30))
    assert end == date(2026, 7, 30)
    assert start == date(2026, 6, 30)


def test_resolve_window_honours_an_explicit_range():
    assert resolve_window("2026-01-01", "2026-03-31", default_days=30) == (
        date(2026, 1, 1),
        date(2026, 3, 31),
    )


def test_resolve_window_derives_start_from_a_given_end():
    start, end = resolve_window(None, "2026-03-31", default_days=10)
    assert (start, end) == (date(2026, 3, 21), date(2026, 3, 31))


def test_resolve_window_rejects_a_reversed_range():
    with pytest.raises(InvalidArgumentError, match="is after"):
        resolve_window("2026-07-30", "2026-07-01", default_days=30)


def test_edge_no_is_normalised_to_lowercase():
    assert require_edge_no(f"  {EDGE_NO.upper()}  ") == EDGE_NO


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-real-id",
        EDGE_NO[:-1],  # 31 chars
        EDGE_NO + "a",  # 33 chars
        "g" * 32,  # right length, not hex
        "",
    ],
)
def test_malformed_edge_no_is_rejected(bad):
    with pytest.raises(InvalidArgumentError, match="edge_no"):
        require_edge_no(bad)
