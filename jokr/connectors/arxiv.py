"""arXiv via its Atom API (https://info.arxiv.org/help/api/).

Parsed only with defusedxml (DTDs, entities and external references refused),
with a depth cap and an element-count cap on top, so a hostile or broken feed
fails fast instead of eating memory. The 3-second spacing arXiv asks for is
enforced by the source's Pacer, not here.
"""

import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, iterparse

from jokr.config import ArxivParams
from jokr.connectors.base import FetchedItem, Rejection
from jokr.guards.tos import GuardedClient

API_URL = "https://export.arxiv.org/api/query"
ABS_URL = "https://arxiv.org/abs/{}"
# arXiv announces papers a day or more after submission; overlap generously.
WATERMARK_OVERLAP = timedelta(days=3)
MAX_DEPTH = 8  # an Atom entry is at most 4 deep
MAX_ELEMENTS = 50_000

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_ID = re.compile(r"arxiv\.org/abs/(?P<id>[^\s?#]+?)(?:v\d+)?$")
_SPACE = re.compile(r"\s+")


class FeedError(ValueError):
    """The feed is not safe or not valid Atom."""


def _squash(text: str | None) -> str:
    return _SPACE.sub(" ", text or "").strip()


def _when(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text.strip()) if text else None
    except ValueError:
        return None


def _entry(elem: Any) -> FetchedItem | Rejection:
    match = _ID.search(_squash(elem.findtext(f"{_ATOM}id")))
    if match is None:
        return Rejection("arxiv:?", "entry without an arXiv id")
    arxiv_id = match.group("id")
    names = [_squash(a.findtext(f"{_ATOM}name")) for a in elem.findall(f"{_ATOM}author")]
    primary = elem.find(f"{_ARXIV}primary_category")
    return FetchedItem(
        external_id=arxiv_id,
        title=_squash(elem.findtext(f"{_ATOM}title")),
        text=_squash(elem.findtext(f"{_ATOM}summary")),
        url=ABS_URL.format(arxiv_id),
        author=next((n for n in names if n), None),
        posted_at=_when(elem.findtext(f"{_ATOM}published")),
        # Built field by field, so no author name ever reaches raw.
        raw={
            "id": arxiv_id,
            "published": elem.findtext(f"{_ATOM}published"),
            "updated": elem.findtext(f"{_ATOM}updated"),
            "primary_category": primary.get("term") if primary is not None else None,
            "categories": [c.get("term") for c in elem.findall(f"{_ATOM}category")],
            "n_authors": len(names),
        },
    )


def parse_feed(data: bytes) -> list[FetchedItem | Rejection]:
    items: list[FetchedItem | Rejection] = []
    depth = count = 0
    try:
        for event, elem in iterparse(BytesIO(data), events=("start", "end"), forbid_dtd=True):
            if event == "start":
                depth += 1
                count += 1
                if depth > MAX_DEPTH:
                    raise FeedError(f"feed nesting over depth {MAX_DEPTH}")
                if count > MAX_ELEMENTS:
                    raise FeedError(f"feed has over {MAX_ELEMENTS} elements")
                continue
            depth -= 1
            if elem.tag == f"{_ATOM}entry":
                items.append(_entry(elem))
                elem.clear()  # keep memory flat on long feeds
    except (DefusedXmlException, ParseError) as exc:
        raise FeedError(f"unsafe or malformed feed: {type(exc).__name__}") from None
    return items


class ArxivConnector:
    def __init__(
        self,
        client: GuardedClient,
        params: ArxivParams,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._params = params
        self._now = now

    async def fetch(self, since: datetime | None) -> AsyncIterator[FetchedItem | Rejection]:
        start = self._now() - timedelta(days=self._params.lookback_days)
        if since is not None:
            start = max(start, since - WATERMARK_OVERLAP)
        query = " OR ".join(f"cat:{c}" for c in self._params.categories)
        offset = 0
        while True:
            response = await self._client.get(
                API_URL,
                params={
                    "search_query": query,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                    "start": offset,
                    "max_results": self._params.page_size,
                },
            )
            response.raise_for_status()
            entries = parse_feed(response.content)
            reached_older = False
            for item in entries:
                if isinstance(item, FetchedItem) and item.posted_at and item.posted_at < start:
                    reached_older = True
                    continue
                yield item
            # Newest first: once a page reaches the window start, older pages are out.
            if reached_older or len(entries) < self._params.page_size:
                return
            offset += self._params.page_size
