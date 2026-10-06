"""C3 in code: GuardedClient only ever talks to a source's allowed hosts.

The check lives in the transport, so it runs on every request, every retry and
every redirect hop. Nothing here touches the network: respx stands in for the
internet and a stub resolver stands in for DNS.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from jokr.guards.tos import (
    MAX_ATTEMPTS,
    GuardedClient,
    GuardError,
    HostNotAllowed,
    ResponseTooLarge,
    SourceDisabled,
    TooManyRedirects,
    UpstreamUnavailable,
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
async def test_redirect_without_location_is_an_error_not_a_response() -> None:
    respx.get(HN).respond(302)
    async with _client() as client:
        with pytest.raises(GuardError, match="Location"):
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


# --- Hardening from the security review --------------------------------------

WWW = "https://www.reddit.com/api/v1/access_token"
OAUTH = "https://oauth.reddit.com/r/SaaS/new"


def _reddit(**kwargs: object) -> GuardedClient:
    source = api_source("reddit", ("www.reddit.com", "oauth.reddit.com"))
    return GuardedClient(source, resolver=public_resolver, sleep=no_sleep, **kwargs)  # type: ignore[arg-type]


@respx.mock
async def test_cross_host_redirect_drops_credentials_cookies_and_keys() -> None:
    respx.get(WWW).respond(302, headers={"Location": OAUTH})
    target = respx.get(OAUTH).respond(200)
    async with _reddit() as client:
        await client.get(
            WWW,
            headers={"Authorization": "Bearer abc", "Cookie": "s=1", "X-API-Key": "k"},
        )
    sent = target.calls.last.request.headers
    for name in ("authorization", "cookie", "x-api-key"):
        assert name not in sent
    assert sent["user-agent"].startswith("JokR-Scout/")


@respx.mock
async def test_same_host_redirect_keeps_headers() -> None:
    respx.get(HN).respond(302, headers={"Location": "/api/v1/search_by_date"})
    target = respx.get("https://hn.algolia.com/api/v1/search_by_date").respond(200)
    async with _client() as client:
        await client.get(HN, headers={"X-Algolia-Agent": "jokr"})
    assert target.calls.last.request.headers["x-algolia-agent"] == "jokr"


@respx.mock
async def test_303_turns_post_into_bodyless_get() -> None:
    respx.post(WWW).respond(303, headers={"Location": "/done"})
    done = respx.get("https://www.reddit.com/done").respond(200)
    async with _reddit() as client:
        await client.post(WWW, data={"grant_type": "client_credentials"})
    sent = done.calls.last.request
    assert sent.method == "GET"
    assert sent.content == b""
    assert "content-type" not in sent.headers


@respx.mock
async def test_307_will_not_resend_a_body_to_another_host() -> None:
    respx.post(WWW).respond(307, headers={"Location": OAUTH})
    other = respx.post(OAUTH).respond(200)
    async with _reddit() as client:
        with pytest.raises(HostNotAllowed, match="body"):
            await client.post(WWW, data={"grant_type": "client_credentials"})
    assert not other.called


@respx.mock
async def test_307_on_the_same_host_keeps_method_and_body() -> None:
    respx.post(WWW).respond(307, headers={"Location": "/api/v1/token2"})
    again = respx.post("https://www.reddit.com/api/v1/token2").respond(200)
    async with _reddit() as client:
        await client.post(WWW, content=b"grant_type=client_credentials")
    assert again.calls.last.request.content == b"grant_type=client_credentials"


@pytest.mark.parametrize(
    "location",
    [
        "//evil.example.com/x",  # protocol-relative
        "https://2130706433/x",  # decimal 127.0.0.1
        "https://0x7f.1/x",
        "https://hn.algolia.com@evil.example.com/x",
    ],
)
async def test_tricky_redirect_targets_are_refused(location: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(HN).respond(302, headers={"Location": location})
        other = router.route(host__regex=r"^(?!hn\.algolia\.com$).*").respond(200)
        async with _client() as client:
            with pytest.raises(HostNotAllowed):
                await client.get(HN)
        assert not other.called


@respx.mock
async def test_malformed_location_is_a_guard_error() -> None:
    respx.get(HN).respond(302, headers={"Location": "https://[::1/x"})
    async with _client() as client:
        with pytest.raises(HostNotAllowed):
            await client.get(HN)


async def test_malformed_url_is_a_guard_error() -> None:
    async with _client() as client:
        with pytest.raises(HostNotAllowed):
            await client.get("https://[::1/x")


@respx.mock
async def test_dns_is_checked_again_on_every_redirect_hop() -> None:
    answers = {"www.reddit.com": ["151.101.1.1"], "oauth.reddit.com": ["10.0.0.9"]}

    async def per_host(host: str) -> list[str]:
        return answers[host]

    respx.get(WWW).respond(302, headers={"Location": OAUTH})
    target = respx.get(OAUTH).respond(200)
    source = api_source("reddit", ("www.reddit.com", "oauth.reddit.com"))
    async with GuardedClient(source, resolver=per_host, sleep=no_sleep) as client:
        with pytest.raises(HostNotAllowed, match="private"):
            await client.get(WWW)
    assert not target.called


async def test_empty_dns_answer_is_refused() -> None:
    async def nothing(host: str) -> list[str]:
        return []

    async with GuardedClient(api_source(), resolver=nothing, sleep=no_sleep) as client:
        with pytest.raises(HostNotAllowed, match="resolve"):
            await client.get(HN)


async def test_dns_failure_is_retried_then_reported_as_unavailable() -> None:
    calls = 0

    async def failing(host: str) -> list[str]:
        nonlocal calls
        calls += 1
        raise OSError("Name or service not known")

    async with GuardedClient(api_source(), resolver=failing, sleep=no_sleep) as client:
        with pytest.raises(UpstreamUnavailable, match="DNS"):
            await client.get(HN)
    assert calls == MAX_ATTEMPTS


async def test_streamed_request_bodies_are_refused() -> None:
    async def body() -> AsyncIterator[bytes]:
        yield b"x"

    async with _client() as client:
        with pytest.raises(GuardError, match="streamed"):
            await client.post(HN, content=body())


def test_client_ignores_proxy_and_netrc_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:8080")
    client = _client()
    assert client._client.trust_env is False


@respx.mock
async def test_scheme_relative_location_stays_on_the_same_host() -> None:
    """RFC 3986 reads "https:evil.example.com/x" as a path on the current host."""
    respx.get(HN).respond(302, headers={"Location": "https:evil.example.com/x"})
    same = respx.get("https://hn.algolia.com/api/v1/evil.example.com/x").respond(200)
    async with _client() as client:
        await client.get(HN)
    assert same.called


@respx.mock
async def test_source_specific_user_agent_is_used() -> None:
    route = respx.get(HN).respond(200)
    async with _client(user_agent="linux:jokr-scout:0.1 (by /u/someone)") as client:
        await client.get(HN)
    assert route.calls.last.request.headers["user-agent"] == "linux:jokr-scout:0.1 (by /u/someone)"
