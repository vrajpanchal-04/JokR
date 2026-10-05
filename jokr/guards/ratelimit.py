"""Per-source request pacing.

A burst of 1 with a minimum gap, not a token bucket: arXiv's terms ask for one
request every three seconds, and a bucket would allow a burst that breaks that.
A hard per-run budget keeps a misbehaving connector from looping forever.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable


class BudgetExhausted(Exception):
    """The source has used its `max_requests` for this run."""


class Pacer:
    def __init__(
        self,
        min_interval_s: float,
        max_requests: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if min_interval_s < 0:
            raise ValueError("min_interval_s must be >= 0")
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        self._interval = min_interval_s
        self._max = max_requests
        self._clock = clock
        self._sleep = sleep
        self._next_at: float | None = None
        self._used = 0
        # Serializes callers so concurrent tasks queue instead of firing together.
        self._lock = asyncio.Lock()

    @property
    def used(self) -> int:
        return self._used

    async def acquire(self) -> None:
        """Wait for this source's next slot, or raise if the run budget is spent."""
        async with self._lock:
            if self._used >= self._max:
                raise BudgetExhausted(f"max_requests={self._max} reached")
            if self._next_at is not None:
                wait = self._next_at - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            self._used += 1
            self._next_at = self._clock() + self._interval

    def defer(self, seconds: float) -> None:
        """Honour a server's Retry-After without ever shortening our own spacing."""
        if seconds <= 0:
            return
        target = self._clock() + seconds
        if self._next_at is None or target > self._next_at:
            self._next_at = target
