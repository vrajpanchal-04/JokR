"""Hacker News via the Algolia search API (https://hn.algolia.com/api).

One pass per (query, tag) plus one per browse tag with no query. Algolia will
not page past 1,000 hits for a search, so a time window with more than that is
split in half until each half fits or reaches an hour.
"""

import html
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from jokr.config import HackerNewsParams
from jokr.connectors.base import FetchedItem, Rejection
from jokr.guards.tos import GuardedClient

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
ALGOLIA_MAX_HITS = 1000
WATERMARK_OVERLAP = timedelta(hours=1)
MIN_WINDOW = timedelta(hours=1)
# Algolia's match markup, useless once stored.
_DROPPED_KEYS = ("_highlightResult", "_snippetResult", "_rankingInfo")

_PARAGRAPH = re.compile(r"<p>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def _plain(fragment: str | None) -> str:
    """Algolia returns HN text as HTML; keep paragraphs, drop markup."""
    if not fragment:
        return ""
    text = _PARAGRAPH.sub("\n\n", fragment)
    return html.unescape(_TAG.sub("", text)).strip()


class HackerNewsConnector:
    def __init__(
        self,
        client: GuardedClient,
        params: HackerNewsParams,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._params = params
        self._now = now

    async def fetch(self, since: datetime | None) -> AsyncIterator[FetchedItem | Rejection]:
        end = self._now()
        start = end - timedelta(days=self._params.lookback_days)
        if since is not None:
            start = max(start, since - WATERMARK_OVERLAP)
        seen: set[str] = set()
        passes = [(q, t) for q in self._params.queries for t in self._params.tags]
        passes += [("", t) for t in self._params.browse_tags]
        for query, tag in passes:
            async for item in self._window(query, tag, start, end):
                key = item.external_id if isinstance(item, FetchedItem) else item.locator
                if key not in seen:
                    seen.add(key)
                    yield item

    async def _window(
        self, query: str, tag: str, start: datetime, end: datetime
    ) -> AsyncIterator[FetchedItem | Rejection]:
        first = await self._page(query, tag, start, end, 0)
        if first.get("nbHits", 0) > ALGOLIA_MAX_HITS and end - start > 2 * MIN_WINDOW:
            # Newest half first, so a budget cut-off loses the oldest items.
            middle = start + (end - start) / 2
            async for item in self._window(query, tag, middle, end):
                yield item
            async for item in self._window(query, tag, start, middle):
                yield item
            return
        for hit in first.get("hits", []):
            yield _to_item(hit)
        pages = min(int(first.get("nbPages", 1)), ALGOLIA_MAX_HITS // self._params.hits_per_page)
        for page in range(1, pages):
            body = await self._page(query, tag, start, end, page)
            for hit in body.get("hits", []):
                yield _to_item(hit)

    async def _page(
        self, query: str, tag: str, start: datetime, end: datetime, page: int
    ) -> dict[str, Any]:
        response = await self._client.get(
            SEARCH_URL,
            params={
                "query": query,
                "tags": tag,
                "numericFilters": (
                    f"created_at_i>={int(start.timestamp())},created_at_i<{int(end.timestamp())}"
                ),
                "hitsPerPage": self._params.hits_per_page,
                "page": page,
            },
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _to_item(hit: Any) -> FetchedItem | Rejection:
    if not isinstance(hit, dict):
        return Rejection("hn:?", "hit is not an object")
    object_id = hit.get("objectID")
    created = hit.get("created_at_i")
    if not isinstance(object_id, str) or not object_id.isdigit():
        return Rejection(f"hn:{str(object_id)[:40]}", "missing or bad objectID")
    if not isinstance(created, int):
        return Rejection(f"hn:{object_id}", "missing created_at_i")
    is_comment = "comment_text" in hit
    hn_link = ITEM_URL.format(object_id)
    author = hit.get("author")
    return FetchedItem(
        external_id=object_id,
        title=hit.get("story_title") if is_comment else hit.get("title"),
        text=_plain(hit.get("comment_text") if is_comment else hit.get("story_text")),
        # Stories keep their external link so the same article dedupes across sources;
        # Ask HN posts and comments point at the discussion itself.
        url=hn_link if is_comment else (hit.get("url") or hn_link),
        author=author if isinstance(author, str) else None,
        points=_int_or_none(hit.get("points")),
        num_comments=_int_or_none(hit.get("num_comments")),
        posted_at=datetime.fromtimestamp(created, UTC),
        raw={k: v for k, v in hit.items() if k not in _DROPPED_KEYS},
    )
