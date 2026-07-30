"""Outbound politeness: token-bucket throttle + single-flight dedup.

This layer protects PSE Edge. It is independent of (and beneath) any per-user
quota enforcement added in the remote deployment.
"""

from __future__ import annotations

import asyncio
import time
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
