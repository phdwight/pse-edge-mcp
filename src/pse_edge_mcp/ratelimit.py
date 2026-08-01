"""Outbound politeness: token-bucket throttle + single-flight dedup, plus the inbound
fixed-window limiter guarding the token endpoint.

`TokenBucket` and `SingleFlight` protect PSE Edge and are independent of (and beneath) any
per-user quota enforcement. `FixedWindowLimiter` faces the other way: it protects *us*, on
the one endpoint where a long-lived credential can be guessed online.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = rate_per_sec
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


class SingleFlight:
    """Concurrent calls for the same key collapse into one upstream request."""

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    async def do(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                fut = existing
            else:
                fut = asyncio.get_running_loop().create_future()
                self._inflight[key] = fut

        if existing is not None:
            return await asyncio.shield(fut)

        try:
            result = await fn()
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            async with self._lock:
                self._inflight.pop(key, None)
            # ensure unconsumed exception doesn't warn
            if fut.done() and fut.exception() is not None:
                fut.exception()


class FixedWindowLimiter:
    """Per-key request ceiling over a fixed window, in process memory.

    Guards `POST /oauth/token`, where a machine client's long-lived secret is the one
    credential an attacker can attack online. Bearer tokens carry full entropy and are
    hopeless to guess; a client secret is equally strong, but the endpoint that checks it
    would otherwise accept unlimited attempts, and rate limiting is what converts "keep
    trying" into "come back later".

    Deliberately the same shape as `QuotaTracker` in auth.py — epoch-aligned fixed windows,
    LRU-bounded key table, counted in process. That means the same caveat: with N workers
    or replicas the effective ceiling is up to N x nominal. That is fine here. The purpose
    is to make online guessing hopeless, not to enforce an exact number, and even N x 20
    attempts a minute against a 48-byte secret is not a threat.

    Keys are checked independently and *all* are counted, so a caller cannot dodge the
    per-IP budget by varying client_id, nor exhaust another client's budget from one host.
    """

    def __init__(
        self,
        *,
        limit: int = 20,
        window_sec: int = 60,
        max_keys: int = 50_000,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._limit = limit
        self._window = window_sec
        self._max_keys = max_keys
        self._time = time_fn or time.time
        self._counts: OrderedDict[str, list[int]] = OrderedDict()  # key -> [window, count]

    def check(self, *keys: str | None) -> int | None:
        """Count this attempt against every supplied key.

        Returns None when allowed, or the seconds until the window rolls when refused —
        which becomes the `Retry-After` header, so a well-behaved client backs off by the
        right amount instead of guessing.
        """
        now = int(self._time())
        window = now // self._window
        retry_after = (window + 1) * self._window - now
        blocked = False

        for key in keys:
            if not key:
                continue
            state = self._counts.get(key)
            if state is None:
                state = [window, 0]
                self._counts[key] = state
                while len(self._counts) > self._max_keys:
                    self._counts.popitem(last=False)
            self._counts.move_to_end(key)
            if state[0] != window:
                state[0], state[1] = window, 0
            state[1] += 1
            # Every key is still counted even once one has tripped: returning early would
            # let an attacker keep a second key's counter cold by always tripping the first.
            if state[1] > self._limit:
                blocked = True

        return retry_after if blocked else None
