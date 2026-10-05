"""Reddit connector: app-only OAuth, token kept out of every log and error."""

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jokr.config import RedditParams
from jokr.connectors.base import FetchedItem
from jokr.connectors.reddit import LISTING_URL, TOKEN_URL, RedditAuthError, RedditConnector
from jokr.guards.redact import Secret
from jokr.guards.tos import GuardedClient, UpstreamUnavailable
from tests.guard_helpers import api_source, no_sleep, public_resolver

LISTING = json.loads((Path(__file__).parent / "fixtures" / "reddit" / "new.json").read_text())
EMPTY = {"kind": "Listing", "data": {"after": None, "children": []}}
NOW = datetime.fromtimestamp(1790000000 + 3600, UTC)
UA = "linux:jokr-scout:0.1 (by /u/fixture_owner)"
# Built at runtime so secret scanners don't flag the fixture.
TOKEN = "fixture" + "-token-" + "Zq81"
CLIENT_SECRET = "fixture" + "-secret-" + "Kp22"
SAAS_NEW = LISTING_URL.format(subreddit="SaaS", listing="new")


def _params(**overrides: Any) -> RedditParams:
    values: dict[str, Any] = {"kind": "reddit", "subreddits": ["SaaS"], "lookback_days": 1}
    values.update(overrides)
    return RedditParams.model_validate(values)


async def _collect(params: RedditParams | None = None) -> list[Any]:
    source = api_source("reddit", ("www.reddit.com", "oauth.reddit.com"))
    async with GuardedClient(
        source, resolver=public_resolver, sleep=no_sleep, user_agent=UA
    ) as client:
        connector = RedditConnector(
            client,
            params or _params(),
            client_id=Secret("fixture-client-id"),
            client_secret=Secret(CLIENT_SECRET),
            now=lambda: NOW,
        )
        return [item async for item in connector.fetch(None)]


def _token_ok() -> respx.Route:
    return respx.post(TOKEN_URL).respond(
        json={"access_token": TOKEN, "token_type": "bearer", "expires_in": 86400}
    )


@respx.mock
async def test_token_request_is_app_only_basic_auth() -> None:
    token = _token_ok()
    respx.get(SAAS_NEW).respond(json=EMPTY)
    await _collect()
    request = token.calls.last.request
    expected = base64.b64encode(f"fixture-client-id:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert request.content == b"grant_type=client_credentials"
    assert request.headers["user-agent"] == UA


@respx.mock
async def test_listing_uses_bearer_token_and_maps_posts() -> None:
    _token_ok()
    listing = respx.get(SAAS_NEW).mock(
        side_effect=[httpx.Response(200, json=LISTING), httpx.Response(200, json=EMPTY)]
    )
    items = await _collect()
    assert listing.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    assert listing.calls[0].request.url.params["limit"] == "100"
    assert listing.calls[1].request.url.params["after"] == "t3_fx0002"

    first, second = items
    assert isinstance(first, FetchedItem)
    assert first.external_id == "t3_fx0001"
    assert first.title == "We pay $400/mo for a billing tool that keeps breaking"
    assert first.url == "https://www.reddit.com/r/SaaS/comments/fx0001/we_pay/"
    assert first.author == "fixture_poster"
    assert (first.points, first.num_comments) == (31, 12)
    assert first.posted_at == datetime.fromtimestamp(1790000000, UTC)
    assert "preview" not in first.raw and "author" not in first.raw
    assert first.raw["subreddit"] == "SaaS"
    assert second.url == "https://example.com/launch?ref=reddit"  # link post keeps its link
    assert second.points is None  # negative score is not engagement


@respx.mock
async def test_paging_stops_at_the_window() -> None:
    _token_ok()
    older = json.loads(json.dumps(LISTING))
    older["data"]["children"][1]["data"]["created_utc"] = (NOW - timedelta(days=3)).timestamp()
    route = respx.get(SAAS_NEW).respond(json=older)
    items = await _collect()
    assert [i.external_id for i in items] == ["t3_fx0001"]
    assert route.call_count == 1


@respx.mock
async def test_page_cap_per_subreddit() -> None:
    _token_ok()
    route = respx.get(SAAS_NEW).respond(json=LISTING)  # always "more"
    params = _params(max_pages_per_subreddit=2, lookback_days=30)
    await _collect(params)
    assert route.call_count == 2


@respx.mock
async def test_bad_credentials_raise_without_secrets(caplog: pytest.LogCaptureFixture) -> None:
    respx.post(TOKEN_URL).respond(401, json={"message": "Unauthorized"})
    with caplog.at_level(logging.DEBUG), pytest.raises(RedditAuthError) as info:
        await _collect()
    text = str(info.value) + repr(info.value) + caplog.text
    assert CLIENT_SECRET not in text
    assert "fixture-client-id" not in text


@respx.mock
async def test_listing_failure_never_leaks_the_token(caplog: pytest.LogCaptureFixture) -> None:
    _token_ok()
    respx.get(SAAS_NEW).respond(503)
    with caplog.at_level(logging.DEBUG), pytest.raises(UpstreamUnavailable) as info:
        await _collect()
    text = str(info.value) + repr(info.value) + caplog.text
    assert TOKEN not in text


@respx.mock
async def test_token_response_without_token_is_an_auth_error() -> None:
    respx.post(TOKEN_URL).respond(json={"error": "invalid_grant"})
    with pytest.raises(RedditAuthError):
        await _collect()


@respx.mock
async def test_children_that_are_not_posts_are_rejected() -> None:
    _token_ok()
    weird = {"data": {"after": None, "children": [{"kind": "t1", "data": {}}, "junk"]}}
    respx.get(SAAS_NEW).respond(json=weird)
    items = await _collect()
    assert len(items) == 2
    assert not any(isinstance(i, FetchedItem) for i in items)
