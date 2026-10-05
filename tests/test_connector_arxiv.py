"""arXiv connector: safe Atom parsing, version-free ids, newest-first paging."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jokr.config import ArxivParams
from jokr.connectors.arxiv import (
    API_URL,
    WATERMARK_OVERLAP,
    ArxivConnector,
    FeedError,
    parse_feed,
)
from jokr.connectors.base import FetchedItem
from jokr.guards.tos import GuardedClient
from tests.guard_helpers import api_source, no_sleep, public_resolver

FEED = (Path(__file__).parent / "fixtures" / "arxiv" / "feed.xml").read_bytes()
NOW = datetime(2026, 10, 5, tzinfo=UTC)
EMPTY = FEED.split(b"<entry>")[0] + b"</feed>"


def _params(**overrides: Any) -> ArxivParams:
    values: dict[str, Any] = {"kind": "arxiv", "categories": ["cs.AI", "cs.LG"], "page_size": 2}
    values.update(overrides)
    return ArxivParams.model_validate(values)


async def _collect(params: ArxivParams, since: datetime | None = None) -> list[Any]:
    source = api_source("arxiv", ("export.arxiv.org",))
    async with GuardedClient(source, resolver=public_resolver, sleep=no_sleep) as client:
        connector = ArxivConnector(client, params, now=lambda: NOW)
        return [item async for item in connector.fetch(since)]


def test_entries_are_parsed() -> None:
    entries = parse_feed(FEED)
    first = entries[0]
    assert isinstance(first, FetchedItem)
    assert first.external_id == "2610.01234"  # version stripped
    assert first.title == "Reducing Latency in Production Data Pipelines"
    assert first.text == "We study pipeline bottlenecks that cost teams revenue."
    assert first.url == "https://arxiv.org/abs/2610.01234"
    assert first.author == "Fixture Author One"
    assert first.posted_at == datetime(2026, 10, 3, 12, tzinfo=UTC)
    assert first.raw["categories"] == ["cs.LG", "cs.AI"]
    assert first.raw["primary_category"] == "cs.LG"
    assert "Fixture Author" not in str(first.raw)


def test_dtd_and_entities_are_refused() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;">]><feed xmlns="http://www.w3.org/2005/Atom">'
        b"<title>&lol2;</title></feed>"
    )
    with pytest.raises(FeedError):
        parse_feed(bomb)


def test_external_entities_are_refused() -> None:
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&e;</title></feed>'
    )
    with pytest.raises(FeedError):
        parse_feed(xxe)


def test_deep_nesting_is_refused() -> None:
    deep = b'<feed xmlns="http://www.w3.org/2005/Atom">' + b"<a>" * 20 + b"</a>" * 20 + b"</feed>"
    with pytest.raises(FeedError, match="depth"):
        parse_feed(deep)


def test_element_flood_is_refused() -> None:
    flood = b'<feed xmlns="http://www.w3.org/2005/Atom">' + b"<x/>" * 60_000 + b"</feed>"
    with pytest.raises(FeedError, match="elements"):
        parse_feed(flood)


def test_malformed_xml_is_a_feed_error() -> None:
    with pytest.raises(FeedError):
        parse_feed(b"<feed><entry></feed>")


def test_entry_without_id_is_rejected_not_fatal() -> None:
    feed = FEED.replace(b"<id>http://arxiv.org/abs/2609.09999v1</id>", b"")
    items = parse_feed(feed)
    assert sum(isinstance(i, FetchedItem) for i in items) == 1
    assert len(items) == 2


@respx.mock
async def test_query_and_paging_stop_at_the_window() -> None:
    route = respx.get(API_URL).mock(
        side_effect=[httpx.Response(200, content=FEED), httpx.Response(200, content=EMPTY)]
    )
    items = await _collect(_params(lookback_days=14))
    # The 2026-09-01 paper is older than 14 days, so paging stops after page one.
    assert [i.external_id for i in items] == ["2610.01234"]
    assert route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["search_query"] == "cat:cs.AI OR cat:cs.LG"
    assert params["sortBy"] == "submittedDate"
    assert params["sortOrder"] == "descending"
    assert (params["start"], params["max_results"]) == ("0", "2")


@respx.mock
async def test_watermark_narrows_the_window() -> None:
    respx.get(API_URL).respond(content=FEED)
    # The window starts WATERMARK_OVERLAP before the watermark, because arXiv
    # announces papers a day or more after their submission date.
    since = datetime(2026, 10, 3, 12, tzinfo=UTC) + WATERMARK_OVERLAP + timedelta(seconds=1)
    items = await _collect(_params(lookback_days=60), since=since)
    assert items == []


@respx.mock
async def test_next_page_is_requested_when_all_entries_are_in_window() -> None:
    route = respx.get(API_URL).mock(
        side_effect=[httpx.Response(200, content=FEED), httpx.Response(200, content=EMPTY)]
    )
    items = await _collect(_params(lookback_days=60))
    assert len(items) == 2
    assert route.calls[1].request.url.params["start"] == "2"
