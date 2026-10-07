"""Hacker News via the Algolia search API (https://hn.algolia.com/api).

One pass per (query, tag) plus one per browse tag with no query. Algolia will
not page past 1,000 hits for a search, so a time window with more than that is
split in half until each half fits or reaches an hour; a window that still has
more is read as far as Algolia allows and reported as truncated.
"""

import html
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from jokr.config import HackerNewsParams
from jokr.connectors.base import FetchedItem, MalformedResponse, Rejection
from jokr.guards.tos import GuardedClient

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
ALGOLIA_MAX_HITS = 1000
WATERMARK_OVERLAP = timedelta(hours=1)
MIN_WINDOW = timedelta(hours=1)
MAX_HTML_CHARS = 128 * 1024  # ingest keeps 32 KB; this bounds the HTML work before that
# raw keeps ids, counts and tags only. Text is already in the text and title
# columns, and the author is hashed, so neither is stored twice.
_RAW_FIELDS = (
    "objectID",
    "created_at",
    "created_at_i",
    "points",
    "num_comments",
    "story_id",
    "parent_id",
    "url",
    "story_url",
)

_PARAGRAPH = re.compile(r"<p>", re.IGNORECASE)
_TAG = re.compile(r"<[^<>]{0,2000}>")


def _plain(fragment: str) -> str:
    """Algolia returns HN text as HTML; keep paragraphs, drop markup."""
    text = _PARAGRAPH.sub("\n\n", fragment[:MAX_HTML_CHARS])
    return html.unescape(_TAG.sub("", text)).strip()


def _count(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MalformedResponse(f"Algolia response has no valid {key}")
    return value


def _hits(body: Any) -> list[Any]:
    hits = body.get("hits") if isinstance(body, dict) else None
    if not isinstance(hits, list):
        raise MalformedResponse("Algolia response has no hits list")
    return hits


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
                # The same story turns up in several passes; keep the first. Every
                # rejection is passed on, so the run counts each one.
                if isinstance(item, FetchedItem):
                    if item.external_id in seen:
                        continue
                    seen.add(item.external_id)
                yield item

    async def _window(
        self, query: str, tag: str, start: datetime, end: datetime
    ) -> AsyncIterator[FetchedItem | Rejection]:
        first = await self._page(query, tag, start, end, 0)
        hits, n_hits = _hits(first), _count(first, "nbHits")
        if n_hits > ALGOLIA_MAX_HITS:
            if end - start > 2 * MIN_WINDOW:
                # Newest half first, so a budget cut-off loses the oldest items.
                middle = start + (end - start) / 2
                async for item in self._window(query, tag, middle, end):
                    yield item
                async for item in self._window(query, tag, start, middle):
                    yield item
                return
            yield Rejection(
                f"hn:{tag}:{query or '*'}"[:300],
                f"{n_hits} hits in {start:%Y-%m-%d %H:%M} to {end:%H:%M}; "
                f"Algolia serves only the first {ALGOLIA_MAX_HITS}",
                incomplete=True,
            )
        for hit in hits:
            yield _to_item(hit)
        pages = min(_count(first, "nbPages"), ALGOLIA_MAX_HITS // self._params.hits_per_page)
        for page in range(1, pages):
            body = await self._page(query, tag, start, end, page)
            for hit in _hits(body):
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
        try:
            body = response.json()
        except ValueError:
            raise MalformedResponse("Algolia response is not JSON") from None
        if not isinstance(body, dict):
            raise MalformedResponse("Algolia response is not an object")
        return body


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any, field: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"{field} is not text")


def _web_url(value: Any) -> str | None:
    if isinstance(value, str) and value.lower().startswith(("https://", "http://")):
        return value
    return None


def _to_item(hit: Any) -> FetchedItem | Rejection:
    if not isinstance(hit, dict):
        return Rejection("hn:?", "hit is not an object")
    object_id = hit.get("objectID")
    if not isinstance(object_id, str) or not object_id.isdigit():
        return Rejection(f"hn:{str(object_id)[:40]}", "missing or bad objectID")
    try:
        return _mapped(object_id, hit)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        # One odd hit is skipped and recorded; it never ends the whole run.
        return Rejection(f"hn:{object_id}", f"malformed hit: {exc}"[:300])


def _mapped(object_id: str, hit: dict[str, Any]) -> FetchedItem:
    created = hit.get("created_at_i")
    if not isinstance(created, int) or isinstance(created, bool):
        raise ValueError("missing created_at_i")
    is_comment = "comment_text" in hit
    title = _str_or_none(hit.get("story_title") if is_comment else hit.get("title"), "title")
    body = _str_or_none(hit.get("comment_text") if is_comment else hit.get("story_text"), "text")
    hn_link = ITEM_URL.format(object_id)
    author = hit.get("author")
    tags = hit.get("_tags")
    raw = {k: hit[k] for k in _RAW_FIELDS if k in hit}
    if isinstance(tags, list):
        raw["_tags"] = [t for t in tags if isinstance(t, str) and not t.startswith("author_")]
    return FetchedItem(
        external_id=object_id,
        title=title,
        text=_plain(body or ""),
        # Stories keep their external link so the same article dedupes across sources;
        # Ask HN posts and comments point at the discussion itself.
        url=hn_link if is_comment else (_web_url(hit.get("url")) or hn_link),
        author=author if isinstance(author, str) else None,
        points=_int_or_none(hit.get("points")),
        num_comments=_int_or_none(hit.get("num_comments")),
        posted_at=datetime.fromtimestamp(created, UTC),
        raw=raw,
    )
