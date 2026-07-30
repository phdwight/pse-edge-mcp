"""Storage protocol + in-memory implementation.

The Postgres implementation (Phase 4) implements the same protocol; entries carry
their fetch timestamp so the market-boundary policy is evaluated at read time —
expiry is a property of the calendar, not of the store.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    fetched_at: datetime


class Storage(Protocol):
    async def get(self, key: str) -> CacheEntry | None: ...

    async def set(self, key: str, entry: CacheEntry) -> None: ...


class InMemoryStorage:
    """Zero-config default for local stdio use."""

    def __init__(self) -> None:
        self._data: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> CacheEntry | None:
        async with self._lock:
            return self._data.get(key)

    async def set(self, key: str, entry: CacheEntry) -> None:
        async with self._lock:
            self._data[key] = entry
