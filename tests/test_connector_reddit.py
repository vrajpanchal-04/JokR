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
from jokr.connectors.base import FetchedItem, MalformedResponse, Rejection
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


# --- review hardening: nothing is lost or admitted without a trace ----------------


@pytest.mark.parametrize(
    "body", [["not", "a", "listing"], {"data": "x"}, {"data": {"after": None}}]
)
@respx.mock
async def test_malformed_listing_fails_loudly(body: Any) -> None:
    _token_ok()
    respx.get(SAAS_NEW).respond(json=body)
    with pytest.raises(MalformedResponse):
        await _collect()


@respx.mock
async def test_token_body_that_is_not_json_is_an_auth_error() -> None:
    respx.post(TOKEN_URL).respond(200, content=b"<html>")
    with pytest.raises(RedditAuthError):
        await _collect()


@respx.mock
async def test_page_cap_is_reported_when_the_window_is_not_reached() -> None:
    _token_ok()
    respx.get(SAAS_NEW).respond(json=LISTING)
    items = await _collect(_params(max_pages_per_subreddit=1, lookback_days=30))
    assert any(isinstance(i, Rejection) and "page cap" in i.reason for i in items)


@respx.mock
async def test_old_stickied_post_does_not_stop_paging() -> None:
    _token_ok()
    page = json.loads(json.dumps(LISTING))
    sticky = page["data"]["children"][0]["data"]
    sticky["stickied"] = True
    sticky["created_utc"] = (NOW - timedelta(days=300)).timestamp()
    route = respx.get(SAAS_NEW).mock(
        side_effect=[httpx.Response(200, json=page), httpx.Response(200, json=EMPTY)]
    )
    await _collect(_params(lookback_days=30))
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"subreddit": "u_someone"}, "profile"),
        ({"created_utc": float("nan")}, "created_utc"),
        ({"created_utc": 1e20}, "created_utc"),
    ],
)
@respx.mock
async def test_bad_posts_become_rejections(change: dict[str, Any], reason: str) -> None:
    _token_ok()
    page = json.loads(json.dumps(EMPTY))
    post = json.loads(json.dumps(LISTING["data"]["children"][0]))
    post["data"].update(change)
    page["data"]["children"] = [post]
    # respx's json= refuses NaN; json.dumps writes it, and the connector's reader accepts it.
    respx.get(SAAS_NEW).respond(
        content=json.dumps(page).encode(), headers={"content-type": "application/json"}
    )
    (item,) = await _collect()
    assert isinstance(item, Rejection)
    assert reason in item.reason


@respx.mock
async def test_non_web_link_falls_back_to_the_discussion() -> None:
    _token_ok()
    page = json.loads(json.dumps(EMPTY))
    post = json.loads(json.dumps(LISTING["data"]["children"][1]))
    post["data"]["url"] = "javascript:alert(1)"
    page["data"]["children"] = [post]
    respx.get(SAAS_NEW).respond(json=page)
    (item,) = await _collect(_params(lookback_days=30))
    assert item.url == "https://www.reddit.com/r/SaaS/comments/fx0002/show/"


@respx.mock
async def test_raw_does_not_duplicate_title_or_body() -> None:
    _token_ok()
    respx.get(SAAS_NEW).respond(json={**LISTING, "data": {**LISTING["data"], "after": None}})
    items = await _collect()
    assert "selftext" not in items[0].raw and "title" not in items[0].raw
