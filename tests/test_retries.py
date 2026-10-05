"""Retries: backoff, Retry-After, and every attempt spends from the pacing budget."""

import httpx
import pytest
import respx

from jokr.guards.ratelimit import BudgetExhausted
from jokr.guards.redact import REDACTED
from jokr.guards.tos import MAX_RETRY_AFTER_S, GuardedClient, UpstreamUnavailable
from tests.guard_helpers import api_source, public_resolver

HN = "https://hn.algolia.com/api/v1/search"
TOKEN = "tok" + "En-" + "a1B2c3D4e5F6g7H8i9J0"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(clock: Clock, *, interval: float = 0.0, max_requests: int = 50) -> GuardedClient:
    return GuardedClient(
        api_source(interval=interval, max_requests=max_requests),
        resolver=public_resolver,
        sleep=clock.sleep,
        clock=clock,
        jitter=lambda: 1.0,
    )


@respx.mock
async def test_503_is_retried_with_exponential_backoff() -> None:
    route = respx.get(HN).mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json={})]
    )
    clock = Clock()
    async with _client(clock) as client:
        response = await client.get(HN)
    assert response.status_code == 200
    assert route.call_count == 3
    assert clock.sleeps == pytest.approx([1.0, 2.0])


@respx.mock
async def test_retry_after_seconds_is_honoured() -> None:
    respx.get(HN).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    clock = Clock()
    async with _client(clock) as client:
        assert (await client.get(HN)).status_code == 200
    assert clock.sleeps == pytest.approx([7.0])


@respx.mock
async def test_retry_after_http_date_is_honoured() -> None:
    respx.get(HN).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}),
            httpx.Response(200),
        ]
    )
    clock = Clock()
    async with _client(clock) as client:
        # A wait far beyond the cap means "come back much later": give up, don't hang.
        with pytest.raises(UpstreamUnavailable, match="Retry-After"):
            await client.get(HN)
    assert clock.sleeps == []


@respx.mock
async def test_retry_after_over_cap_gives_up() -> None:
    respx.get(HN).respond(429, headers={"Retry-After": str(MAX_RETRY_AFTER_S + 1)})
    async with _client(Clock()) as client:
        with pytest.raises(UpstreamUnavailable):
            await client.get(HN)


@respx.mock
async def test_retry_never_shortens_the_source_interval() -> None:
    respx.get(HN).mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    clock = Clock()
    async with _client(clock, interval=3.0) as client:
        await client.get(HN)
    assert clock.sleeps == pytest.approx([3.0])


@respx.mock
async def test_persistent_failure_raises_after_max_attempts() -> None:
    route = respx.get(HN).respond(502)
    async with _client(Clock()) as client:
        with pytest.raises(UpstreamUnavailable, match="502"):
            await client.get(HN)
    assert route.call_count == 4


@respx.mock
async def test_client_errors_are_not_retried() -> None:
    route = respx.get(HN).respond(404)
    async with _client(Clock()) as client:
        assert (await client.get(HN)).status_code == 404
    assert route.call_count == 1


@respx.mock
async def test_connection_errors_are_retried() -> None:
    route = respx.get(HN).mock(side_effect=[httpx.ConnectError("reset"), httpx.Response(200)])
    async with _client(Clock()) as client:
        assert (await client.get(HN)).status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_retries_count_toward_the_run_budget() -> None:
    respx.get(HN).respond(503)
    async with _client(Clock(), max_requests=2) as client:
        with pytest.raises(BudgetExhausted):
            await client.get(HN)


@respx.mock
async def test_errors_never_carry_the_authorization_header() -> None:
    respx.get(HN).mock(side_effect=httpx.ConnectError(f"failed with Bearer {TOKEN}"))
    async with _client(Clock()) as client:
        with pytest.raises(UpstreamUnavailable) as info:
            await client.get(HN, headers={"Authorization": f"Bearer {TOKEN}"})
    err = info.value
    assert TOKEN not in str(err)
    assert TOKEN not in repr(err)
    assert REDACTED in str(err)
    # The original exception (which holds the request and its headers) is not chained.
    assert err.__cause__ is None
    assert err.__suppress_context__
