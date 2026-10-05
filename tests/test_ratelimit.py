"""Pacing: requests per source are spaced and capped (C3, arXiv terms)."""

import asyncio
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jokr.guards.ratelimit import BudgetExhausted, Pacer


class FakeClock:
    """A clock that only moves when the pacer sleeps, so tests are exact and instant."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _pacer(interval: float, max_requests: int = 100) -> tuple[Pacer, FakeClock]:
    clock = FakeClock()
    return Pacer(interval, max_requests, clock=clock, sleep=clock.sleep), clock


async def test_first_request_does_not_wait() -> None:
    pacer, clock = _pacer(3.0)
    await pacer.acquire()
    assert clock.sleeps == []


async def test_requests_are_spaced_by_the_interval() -> None:
    pacer, clock = _pacer(3.0)
    times = []
    for _ in range(4):
        await pacer.acquire()
        times.append(clock.now)
    gaps = [b - a for a, b in pairwise(times)]
    assert gaps == pytest.approx([3.0, 3.0, 3.0])


async def test_no_wait_when_enough_time_already_passed() -> None:
    pacer, clock = _pacer(3.0)
    await pacer.acquire()
    clock.now += 10
    await pacer.acquire()
    assert clock.sleeps == []


async def test_concurrent_callers_are_still_spaced() -> None:
    """Burst of 1: concurrent tasks queue, they don't fire together."""
    pacer, clock = _pacer(2.0)
    stamps: list[float] = []

    async def hit() -> None:
        await pacer.acquire()
        stamps.append(clock.now)

    await asyncio.gather(*(hit() for _ in range(5)))
    stamps.sort()
    assert [b - a for a, b in pairwise(stamps)] == pytest.approx([2.0] * 4)


async def test_budget_is_a_hard_cap() -> None:
    pacer, _ = _pacer(0.0, max_requests=3)
    for _ in range(3):
        await pacer.acquire()
    with pytest.raises(BudgetExhausted):
        await pacer.acquire()
    assert pacer.used == 3


async def test_defer_pushes_the_next_request_back() -> None:
    """Retry-After: the server's wait wins when it is longer than our interval."""
    pacer, clock = _pacer(1.0)
    await pacer.acquire()
    start = clock.now
    pacer.defer(30.0)
    await pacer.acquire()
    assert clock.now - start == pytest.approx(30.0)


async def test_defer_never_shortens_the_interval() -> None:
    pacer, clock = _pacer(5.0)
    await pacer.acquire()
    start = clock.now
    pacer.defer(0.5)
    await pacer.acquire()
    assert clock.now - start == pytest.approx(5.0)


@pytest.mark.parametrize(("interval", "max_requests"), [(-1.0, 1), (1.0, 0)])
def test_invalid_settings_are_rejected(interval: float, max_requests: int) -> None:
    with pytest.raises(ValueError, match=r"interval|max_requests"):
        Pacer(interval, max_requests)


@given(
    interval=st.floats(min_value=0, max_value=60, allow_nan=False),
    n=st.integers(min_value=1, max_value=30),
)
def test_spacing_property(interval: float, n: int) -> None:
    """For any interval and request count, no two requests are closer than the interval."""

    async def run() -> list[float]:
        pacer, clock = _pacer(interval, max_requests=n)
        out = []
        for _ in range(n):
            await pacer.acquire()
            out.append(clock.now)
        return out

    stamps = asyncio.run(run())
    for a, b in pairwise(stamps):
        assert b - a >= interval - 1e-9
