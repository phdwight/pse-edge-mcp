"""In-process memo for parsed results.

The cache layer stores what PSE Edge returned — HTML, mostly — so every request re-parses
the same bytes into the same objects. Measured on real fixtures: 2.17 ms for the homepage
and 1.03 ms for a disclosure page, against 0.04 ms to build the models and 0.04 ms to
serialise them. Parsing *is* the request cost, and it scales with users while producing an
identical answer for all of them.

**This memo is safe by construction, not by a TTL guess.** A cached upstream value cannot
change before its freeze boundary, so nothing derived from it can either. The validity token
is the entry's `as_of`: `FreezeService` reports the exact timestamp the underlying value was
fetched, and a new fetch necessarily carries a new one, so a boundary crossing misses the
memo automatically. There is no invalidation logic to get wrong and no staleness window.

Deliberately in-process rather than in Postgres: the point is to avoid pulling a 42 KB blob
over the wire and re-parsing it, so pushing the derived value back into the database would
defeat it. Each replica memoises independently, which is fine — the entries are pure
functions of data all replicas agree on.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .service import Served

DEFAULT_MAX_ENTRIES = 512


class ParsedMemo:
    """Bounded LRU of parsed values, keyed by projection and validated by `as_of`."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._entries: OrderedDict[str, tuple[datetime, Any]] = OrderedDict()
        self._max_entries = max_entries
        self.hits = 0
        self.misses = 0

    def resolve[T](
        self, memo_key: str, served: Served[Any], parse: Callable[[Any], T]
    ) -> Served[T]:
        """Return `parse(served.value)`, reusing a previous result when still valid.

        `memo_key` must identify the **projection**, not just the source: `get_indices` and
        `get_market_summary` read the same cached homepage under one cache key but parse it
        into different shapes, so a shared key would hand one tool the other's result.
        Call sites suffix the cache key accordingly (`homepage#indices`).

        Metadata always comes from `served`, never from the memo, so `from_cache` and
        `stale` keep describing the upstream fetch rather than this local reuse.
        """
        cached = self._entries.get(memo_key)
        if cached is not None and cached[0] == served.meta.as_of:
            self._entries.move_to_end(memo_key)
            self.hits += 1
            return Served(value=cached[1], meta=served.meta)

        value = parse(served.value)
        self.misses += 1
        self._entries[memo_key] = (served.meta.as_of, value)
        self._entries.move_to_end(memo_key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return Served(value=value, meta=served.meta)

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "entries": len(self._entries)}
