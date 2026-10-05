"""Hacker News (Algolia) connector: maps hits, pages, and splits windows over 1,000 hits."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import respx

from jokr.config import HackerNewsParams
from jokr.connectors.base import FetchedItem
from jokr.connectors.hn import ALGOLIA_MAX_HITS, SEARCH_URL, HackerNewsConnector
from jokr.guards.tos import GuardedClient
from tests.guard_helpers import api_source, no_sleep, public_resolver

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "hn" / "search_page.json").read_text())
NOW = datetime(2026, 10, 5, tzinfo=UTC)


def _params(**overrides: Any) -> HackerNewsParams:
    values: dict[str, Any] = {
        "kind": "hackernews",
        "queries": ["is there a tool"],
        "tags": ["story"],
        "browse_tags": [],
        "hits_per_page": 100,
        "lookback_days": 30,
    }
    values.update(overrides)
    return HackerNewsParams.model_validate(values)


def _client(max_requests: int = 50) -> GuardedClient:
    return GuardedClient(
        api_source(max_requests=max_requests), resolver=public_resolver, sleep=no_sleep
    )


async def _collect(params: HackerNewsParams, since: datetime | None = None) -> list[Any]:
    async with _client() as client:
        connector = HackerNewsConnector(client, params, now=lambda: NOW)
        return [item async for item in connector.fetch(since)]


@respx.mock
async def test_hits_are_mapped_to_items() -> None:
    respx.get(SEARCH_URL).respond(json=FIXTURE)
    items = await _collect(_params())
    by_id = {i.external_id: i for i in items}
    ask = by_id["41000001"]
    assert isinstance(ask, FetchedItem)
    assert ask.title == "Ask HN: Is there a tool for reconciling payroll across countries?"
    assert ask.text == "We built our own internal tool for this & it's painful.\n\nBudget is there."
    assert ask.url == "https://news.ycombinator.com/item?id=41000001"
    assert ask.author == "fixture_user_a"
    assert (ask.points, ask.num_comments) == (42, 17)
    assert ask.posted_at == datetime.fromtimestamp(1790000000, UTC)
    assert "_highlightResult" not in ask.raw

    show = by_id["41000002"]
    assert show.url == "https://www.example.com/product/?utm_source=hn"
    assert show.text == ""

    comment = by_id["41000003"]
    assert comment.title == "Ask HN: Is there a tool for reconciling payroll across countries?"
    assert comment.url == "https://news.ycombinator.com/item?id=41000003"
    assert comment.points is None


@respx.mock
async def test_query_tags_and_time_window_are_sent() -> None:
    route = respx.get(SEARCH_URL).respond(json=FIXTURE)
    since = NOW - timedelta(days=2)
    await _collect(_params(), since=since)
    params = route.calls.last.request.url.params
    assert params["query"] == "is there a tool"
    assert params["tags"] == "story"
    assert params["hitsPerPage"] == "100"
    lower = int((since - timedelta(hours=1)).timestamp())  # watermark overlap
    assert params["numericFilters"] == f"created_at_i>={lower},created_at_i<{int(NOW.timestamp())}"


@respx.mock
async def test_first_run_uses_lookback() -> None:
    route = respx.get(SEARCH_URL).respond(json=FIXTURE)
    await _collect(_params(lookback_days=7))
    lower = int((NOW - timedelta(days=7)).timestamp())
    assert f"created_at_i>={lower}" in route.calls.last.request.url.params["numericFilters"]


@respx.mock
async def test_browse_tags_run_without_a_query() -> None:
    route = respx.get(SEARCH_URL).respond(json={**FIXTURE, "hits": []})
    await _collect(_params(queries=["x"], tags=["story"], browse_tags=["ask_hn", "show_hn"]))
    sent = [(c.request.url.params.get("query"), c.request.url.params["tags"]) for c in route.calls]
    assert ("", "ask_hn") in sent
    assert ("", "show_hn") in sent


@respx.mock
async def test_duplicate_hits_across_passes_are_yielded_once() -> None:
    respx.get(SEARCH_URL).respond(json=FIXTURE)
    items = await _collect(_params(queries=["a", "b"]))
    ids = [i.external_id for i in items]
    assert len(ids) == len(set(ids)) == 3


@respx.mock
async def test_pages_are_followed() -> None:
    page0 = {**FIXTURE, "hits": FIXTURE["hits"][:1], "nbPages": 2, "nbHits": 2}
    page1 = {**FIXTURE, "hits": FIXTURE["hits"][1:2], "nbPages": 2, "nbHits": 2, "page": 1}

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page1 if request.url.params["page"] == "1" else page0)

    respx.get(SEARCH_URL).mock(side_effect=reply)
    items = await _collect(_params())
    assert [i.external_id for i in items] == ["41000001", "41000002"]


@respx.mock
async def test_window_over_algolia_limit_is_split() -> None:
    windows: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        nf = request.url.params["numericFilters"]
        windows.append(nf)
        lo, hi = (int(part.split("=")[-1].split("<")[-1]) for part in nf.split(","))
        too_many = hi - lo > 4 * 86400
        body = {**FIXTURE, "hits": [], "nbHits": ALGOLIA_MAX_HITS + 1 if too_many else 0}
        return httpx.Response(200, json=body)

    respx.get(SEARCH_URL).mock(side_effect=reply)
    await _collect(_params(lookback_days=16))
    # 16 days -> 8 -> 4: the first two windows were split, the 4-day ones were not.
    assert len(windows) == 1 + 2 + 4


@respx.mock
async def test_malformed_hits_become_rejections() -> None:
    bad = {**FIXTURE, "hits": [{"objectID": "1"}, {"created_at_i": 1}, FIXTURE["hits"][0]]}
    respx.get(SEARCH_URL).respond(json=bad)
    items = await _collect(_params())
    rejected = [i for i in items if not isinstance(i, FetchedItem)]
    assert len(rejected) == 2
    assert all(r.locator.startswith("hn:") for r in rejected)
