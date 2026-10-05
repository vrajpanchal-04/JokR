"""Reddit via its official Data API, app-only OAuth (no user login).

The token comes from www.reddit.com and data from oauth.reddit.com; both hosts
are on the source's allowlist. The token lives only in a `Secret`, goes only
into an Authorization header, and never into a URL, log line or exception.
Reddit stays disabled until Luca adds API keys and Reddit approves commercial use.
"""

import base64
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from jokr.config import RedditParams
from jokr.connectors.base import FetchedItem, Rejection
from jokr.guards.redact import Secret
from jokr.guards.tos import GuardedClient

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 - an endpoint, not a secret
LISTING_URL = "https://oauth.reddit.com/r/{subreddit}/{listing}"
SITE = "https://www.reddit.com"
WATERMARK_OVERLAP = timedelta(hours=1)
# raw keeps only these post fields: no author data, previews or award noise.
_RAW_FIELDS = (
    "id",
    "name",
    "subreddit",
    "title",
    "selftext",
    "is_self",
    "url",
    "permalink",
    "score",
    "num_comments",
    "upvote_ratio",
    "created_utc",
    "link_flair_text",
    "over_18",
)


class RedditAuthError(Exception):
    """Reddit refused the app credentials. The message never contains them."""


class RedditConnector:
    def __init__(
        self,
        client: GuardedClient,
        params: RedditParams,
        *,
        client_id: Secret,
        client_secret: Secret,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._params = params
        self._client_id = client_id
        self._client_secret = client_secret
        self._now = now

    async def _authenticate(self) -> Secret:
        pair = f"{self._client_id.reveal()}:{self._client_secret.reveal()}"
        response = await self._client.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {base64.b64encode(pair.encode()).decode()}"},
            data={"grant_type": "client_credentials"},
        )
        if response.status_code != 200:
            raise RedditAuthError(f"token request refused: HTTP {response.status_code}")
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise RedditAuthError("token response had no access_token")
        return Secret(token)

    async def fetch(self, since: datetime | None) -> AsyncIterator[FetchedItem | Rejection]:
        start = self._now() - timedelta(days=self._params.lookback_days)
        if since is not None:
            start = max(start, since - WATERMARK_OVERLAP)
        token = await self._authenticate()
        for subreddit in self._params.subreddits:
            async for item in self._subreddit(subreddit, start, token):
                yield item

    async def _subreddit(
        self, subreddit: str, start: datetime, token: Secret
    ) -> AsyncIterator[FetchedItem | Rejection]:
        url = LISTING_URL.format(subreddit=subreddit, listing=self._params.listing)
        after: str | None = None
        for _ in range(self._params.max_pages_per_subreddit):
            query: dict[str, Any] = {"limit": self._params.limit, "raw_json": 1}
            if after:
                query["after"] = after
            response = await self._client.get(
                url, params=query, headers={"Authorization": f"Bearer {token.reveal()}"}
            )
            response.raise_for_status()
            data = response.json().get("data") or {}
            reached_older = False
            for child in data.get("children") or []:
                item = _to_item(child, subreddit)
                if isinstance(item, FetchedItem) and item.posted_at and item.posted_at < start:
                    reached_older = True
                    continue
                yield item
            after = data.get("after")
            if reached_older or not isinstance(after, str):
                return


def _to_item(child: Any, subreddit: str) -> FetchedItem | Rejection:
    data = child.get("data") if isinstance(child, dict) else None
    if not isinstance(child, dict) or child.get("kind") != "t3" or not isinstance(data, dict):
        return Rejection(f"reddit:r/{subreddit}", "listing child is not a post")
    name, created = data.get("name"), data.get("created_utc")
    if not isinstance(name, str) or not name.startswith("t3_"):
        return Rejection(f"reddit:r/{subreddit}", "post without a t3_ fullname")
    if not isinstance(created, int | float):
        return Rejection(f"reddit:{name}", "post without created_utc")
    permalink = data.get("permalink")
    discussion = f"{SITE}{permalink}" if isinstance(permalink, str) else None
    link = data.get("url") if not data.get("is_self") else None
    score, comments = data.get("score"), data.get("num_comments")
    author, title, body = data.get("author"), data.get("title"), data.get("selftext")
    return FetchedItem(
        external_id=name,
        title=title if isinstance(title, str) else None,
        text=body if isinstance(body, str) else "",
        # Link posts keep their link so the same article dedupes across sources.
        url=link if isinstance(link, str) and link else discussion,
        author=author if isinstance(author, str) and author != "[deleted]" else None,
        points=score if isinstance(score, int) and score >= 0 else None,
        num_comments=comments if isinstance(comments, int) and comments >= 0 else None,
        posted_at=datetime.fromtimestamp(created, UTC),
        raw={k: data[k] for k in _RAW_FIELDS if k in data},
    )
