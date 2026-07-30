"""Postgres implementation of the `Archive` protocol.

Split from `archive.py` so SQLAlchemy is imported only when `DATABASE_URL` is set — the
protocol and its no-op default must stay importable on a install without the `postgres`
extra.

See `archive.py` for why a failed write is logged rather than raised.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .db import disclosures as disclosures_table
from .db import eod_bars
from .models import DisclosureHit, OhlcBar

logger = logging.getLogger(__name__)


class PostgresArchive:
    """Upserts whatever passes through into the archive tables."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record_bars(
        self, *, company_id: str, security_id: str, symbol: str | None, bars: list[OhlcBar]
    ) -> None:
        if not bars:
            return
        rows = [
            {
                "company_id": company_id,
                "security_id": security_id,
                "trade_date": bar.trade_date,
                "symbol": symbol,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "value": bar.value,
            }
            for bar in bars
        ]
        # A closed bar is immutable, so a re-fetch of an overlapping range must not churn
        # rows or move first_seen_at: conflicts are ignored, and the earliest sighting of
        # each bar is preserved.
        await self._upsert_ignore(eod_bars, rows, ["company_id", "security_id", "trade_date"])

    async def record_disclosures(self, hits: list[DisclosureHit]) -> None:
        if not hits:
            return
        rows: list[dict[str, Any]] = [
            {
                "edge_no": edge_no,
                "company_id": hit.company_id,
                "company_name": hit.company_name,
                "template": hit.template,
                "announced_at": hit.announced_at,
                "pse_form_number": hit.pse_form_number,
                "circular_number": hit.circular_number,
            }
            for hit in hits
            if (edge_no := hit.edge_no)
        ]
        # Deduplicate within the batch: one page can legitimately repeat an edge_no, and
        # Postgres rejects a statement that touches the same key twice.
        unique: dict[str, dict[str, Any]] = {str(row["edge_no"]): row for row in rows}
        await self._upsert_ignore(disclosures_table, list(unique.values()), ["edge_no"])

    async def _upsert_ignore(
        self, table: Any, rows: list[dict[str, Any]], conflict_on: list[str]
    ) -> None:
        if not rows:
            return
        stmt = pg_insert(table).values(rows).on_conflict_do_nothing(index_elements=conflict_on)
        try:
            async with self._engine.begin() as conn:
                await conn.execute(stmt)
        except Exception:
            # Deliberately broad. `SQLAlchemyError` alone is not enough: a database that is
            # down or unreachable surfaces as OSError/ConnectionRefusedError from the driver
            # socket, which would then propagate and break the user's read — the one thing
            # this module promises never to do. `Exception` (not BaseException) still lets
            # asyncio.CancelledError through, so task cancellation is unaffected.
            logger.warning("archive write to %s failed; continuing", table.name, exc_info=True)
