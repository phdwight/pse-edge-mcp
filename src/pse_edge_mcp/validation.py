"""Argument validation for tool inputs.

Each function does one thing, raises `InvalidArgumentError`, and is reusable across
tools. Previously every tool open-coded these checks, so the same rule was written
several times and drifted — one tool rejected a reversed date range, another did not.

Validation happens before any upstream call, so a malformed argument never costs PSE
Edge a request.
"""

from __future__ import annotations

from datetime import date, timedelta

from .errors import InvalidArgumentError
from .parsers import EDGE_NO_RE


def require_page(page: int) -> int:
    if page < 1:
        raise InvalidArgumentError(f"page must be 1 or greater, got {page}")
    return page


def require_limit(value: int, field: str, *, lo: int = 1, hi: int = 100) -> int:
    """Bound a caller-supplied result cap: big enough to be useful, small enough that a
    model asking for 'everything' cannot turn one reply into a megabyte."""
    if not lo <= value <= hi:
        raise InvalidArgumentError(f"{field} must be between {lo} and {hi}, got {value}")
    return value


def require_text(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise InvalidArgumentError(f"{field} must not be empty")
    return stripped


def optional_date(value: str | None, field: str) -> date | None:
    """Parse an ISO date, surfacing a malformed one as INVALID_ARGUMENT.

    `date.fromisoformat` raises ValueError, which would otherwise escape as an
    unhelpful INTERNAL_ERROR — the caller needs to know *which* argument was wrong.
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidArgumentError(
            f"{field} must be an ISO date (YYYY-MM-DD), got '{value}' ({exc})"
        ) from exc


def require_ordered(start: date | None, end: date | None) -> None:
    if start and end and start > end:
        raise InvalidArgumentError(
            f"start_date {start.isoformat()} is after end_date {end.isoformat()}"
        )


def resolve_window(
    start_date: str | None, end_date: str | None, *, default_days: int, today: date | None = None
) -> tuple[date, date]:
    """Resolve an optional ISO date range into a concrete, ordered window.

    `today` is injectable so tests do not depend on the day they run.
    """
    end = optional_date(end_date, "end_date") or (today or date.today())
    start = optional_date(start_date, "start_date") or end - timedelta(days=default_days)
    require_ordered(start, end)
    return start, end


def require_file_id(raw: str) -> str:
    """Validate an attachment file_id (numeric, from the disclosure viewer).

    The id is interpolated into an upstream request, so anything but digits is refused —
    the resource endpoint must not become a proxy for arbitrary PSE Edge queries.
    """
    key = raw.strip()
    if not key.isdigit() or len(key) > 12:
        raise InvalidArgumentError(
            f"file_id must be the numeric id from get_disclosure's attachments, got '{raw}'"
        )
    return key


def require_edge_no(raw: str) -> str:
    """Normalise and validate a 32-hex disclosure key."""
    key = raw.strip().lower()
    if not EDGE_NO_RE.match(key):
        raise InvalidArgumentError(
            f"edge_no must be a 32-character hex id from search_disclosures, got '{raw}'"
        )
    return key
