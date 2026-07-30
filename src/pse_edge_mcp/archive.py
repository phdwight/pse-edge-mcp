"""Opportunistic EOD archive (plan §6a).

Rows accrue **only from fetches a user already triggered**. There is no crawler and no
nightly sync, so the archive deepens over time at zero extra cost to PSE Edge — which
matters because Edge serves limited history and the freeze policy exists to protect it.

Two implementations behind one protocol: `NullArchive` here (the stdio default — archiving
is a Postgres-only benefit) and `PostgresArchive` in `archive_postgres.py`. Repositories
depend on the protocol, so nothing above this layer knows whether an archive exists.

**This module must not import SQLAlchemy.** It is on the import path of every install,
including a plain `pip install pse-edge-mcp` without the `postgres` extra, so the Postgres
implementation lives in its own module and is imported only when DATABASE_URL is set.

**A failed archive write never fails the user's request.** Recording is bookkeeping that
happens to ride along on a read; if the database rejects it, the caller should still get
their data. Errors are logged, not raised. That is a deliberate departure from invariant #4
(be loud on drift), which is about PSE Edge changing shape underneath us — a different
problem from a write we can safely lose.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .models import DisclosureHit, OhlcBar

logger = logging.getLogger(__name__)


class Archive(Protocol):
    async def record_bars(
        self, *, company_id: str, security_id: str, symbol: str | None, bars: list[OhlcBar]
    ) -> None: ...

    async def record_disclosures(self, hits: list[DisclosureHit]) -> None: ...


class NullArchive:
    """No-op default. Local stdio use gets caching without a database."""

    async def record_bars(
        self, *, company_id: str, security_id: str, symbol: str | None, bars: list[OhlcBar]
    ) -> None:
        return None

    async def record_disclosures(self, hits: list[DisclosureHit]) -> None:
        return None
