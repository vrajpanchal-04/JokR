"""C3 in code: GuardedClient only ever talks to a source's allowed hosts.

The check lives in the transport, so it runs on every request, every retry and
every redirect hop. Nothing here touches the network: respx stands in for the
internet and a stub resolver stands in for DNS.
"""

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from jokr.guards.tos import (
    GuardedClient,
    HostNotAllowed,
    ResponseTooLarge,
    SourceDisabled,
    TooManyRedirects,
    check_url,
)
from tests.guard_helpers import api_source, no_sleep, public_resolver

HN = "https://hn.algolia.com/api/v1/search"


def _client(**kwargs: object) -> GuardedClient:
    source = kwargs.pop("source", None) or api_source()
    return GuardedClient(source, resolver=public_resolver, sleep=no_sleep, **kwargs)  # type: ignore[arg-type]


@respx.mock
async def test_allowed_host_is_reached() -> None:
    respx.get(HN).respond(json={"hits": []})
    async with _client() as client:
        response = await client.get(HN)
    assert response.status_code == 200
    assert response.json() == {"hits": []}


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/x",  # unlisted host
        "https://hn.algolia.com.evil.com/x",  # suffix trick
        "https://evilhn.algolia.com/x",  # sibling subdomain
        "http://hn.algolia.com/x",  # not HTTPS
        "https://hn.algolia.com:8443/x",  # not 443
        "https://user:pw@hn.algolia.com/x",  # userinfo
        "https://127.0.0.1/x",  # IP literal
        "https://[::1]/x",  # IPv6 literal
        "ftp://hn.algolia.com/x",  # other scheme
    ],
)
async def test_disallowed_urls_are_refused_before_any_request(url: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.route().respond(200)
        async with _client() as client:
            with pytest.raises(HostNotAllowed):
                await client.get(url)
        assert not route.called


def test_check_url_accepts_exact_host_case_and_trailing_dot_insensitively() -> None:
    allowed = frozenset({"hn.algolia.com"})
    check_url(httpx.URL("https://HN.Algolia.com/x"), allowed)
    check_url(httpx.URL("https://hn.algolia.com./x"), allowed)
    check_url(httpx.URL("https://hn.algolia.com:443/x"), allowed)


async def test_disabled_source_cannot_get_a_client() -> None:
    with pytest.raises(SourceDisabled):
        _client(source=api_source(enabled=False))


async def test_host_resolving_to_private_address_is_refused() -> None:
    async def internal(host: str) -> list[str]:
        return ["10.0.0.5"]

    with respx.mock(assert_all_called=False) as router:
        route = router.get(HN).respond(200)
        async with GuardedClient(api_source(), resolver=internal, sleep=no_sleep) as client:
            with pytest.raises(HostNotAllowed, match="private"):
                await client.get(HN)
        assert not route.called


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "fd00::1"])
async def test_any_private_answer_in_the_dns_set_is_refused(ip: str) -> None:
    async def mixed(host: str) -> list[str]:
        return ["151.101.1.1", ip]

    async with GuardedClient(api_source(), resolver=mixed, sleep=no_sleep) as client:
        with pytest.raises(HostNotAllowed):
            await client.get(HN)


@respx.mock
async def test_redirect_to_unlisted_host_is_refused() -> None:
    respx.get(HN).respond(302, headers={"Location": "https://evil.example.com/steal"})
    evil = respx.get("https://evil.example.com/steal").respond(200)
    async with _client() as client:
        with pytest.raises(HostNotAllowed):
            await client.get(HN)
    assert not evil.called


@respx.mock
async def test_redirect_to_metadata_ip_is_refused() -> None:
    respx.get(HN).respond(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
    async with _client() as client:
        with pytest.raises(HostNotAllowed):
            await client.get(HN)


@respx.mock
async def test_redirect_within_allowed_host_is_followed() -> None:
    respx.get(HN).respond(301, headers={"Location": "/api/v1/search_by_date"})
    respx.get("https://hn.algolia.com/api/v1/search_by_date").respond(json={"ok": True})
    async with _client() as client:
        response = await client.get(HN)
    assert response.json() == {"ok": True}


@respx.mock
async def test_redirect_loop_is_capped() -> None:
    respx.get(HN).respond(302, headers={"Location": HN})
    async with _client() as client:
        with pytest.raises(TooManyRedirects):
            await client.get(HN)


@respx.mock
async def test_oversized_response_is_aborted() -> None:
    respx.get(HN).respond(200, content=b"x" * 2048)
    async with _client(max_response_bytes=1024) as client:
        with pytest.raises(ResponseTooLarge):
            await client.get(HN)


@respx.mock
async def test_oversized_content_length_is_refused_up_front() -> None:
    respx.get(HN).respond(200, content=b"x", headers={"Content-Length": "999999999"})
    async with _client(max_response_bytes=1024) as client:
        with pytest.raises(ResponseTooLarge):
            await client.get(HN)


@respx.mock
async def test_requests_carry_an_honest_user_agent() -> None:
    route = respx.get(HN).respond(200)
    async with _client() as client:
        await client.get(HN)
    assert route.calls.last.request.headers["user-agent"].startswith("JokR-Scout/")


@respx.mock
async def test_every_request_spends_from_the_run_budget() -> None:
    respx.get(HN).respond(200)
    async with _client(source=api_source(max_requests=2)) as client:
        await client.get(HN)
        await client.get(HN)
        with pytest.raises(Exception, match="max_requests"):
            await client.get(HN)


@given(host=st.from_regex(r"[a-z]{1,10}\.(com|net|org)", fullmatch=True))
def test_check_url_property_only_exact_host_passes(host: str) -> None:
    allowed = frozenset({"hn.algolia.com"})
    if host == "hn.algolia.com":
        return
    with pytest.raises(HostNotAllowed):
        check_url(httpx.URL(f"https://{host}/x"), allowed)
