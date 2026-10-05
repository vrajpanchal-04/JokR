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
from jokr.connectors.base import FetchedItem, MalformedResponse, Rejection
from jokr.guards.redact import Secret
from jokr.guards.tos import GuardedClient

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 - an endpoint, not a secret
LISTING_URL = "https://oauth.reddit.com/r/{subreddit}/{listing}"
SITE = "https://www.reddit.com"
WATERMARK_OVERLAP = timedelta(hours=1)
# raw keeps only these post fields: no author data, previews or award noise, and
# not the title or body, which are already in their own columns.
_RAW_FIELDS = (
    "id",
    "name",
    "subreddit",
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
        try:
            body = response.json()
        except ValueError:
            raise RedditAuthError("token response is not JSON") from None
        token = body.get("access_token") if isinstance(body, dict) else None
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
            children, after = _listing(response)
            reached_older = False
            for child in children:
                item = _to_item(child, subreddit)
                too_old = isinstance(item, FetchedItem) and (
                    item.posted_at is not None and item.posted_at < start
                )
                if too_old:
                    # Pinned posts sit on top whatever their age, so only an
                    # ordinary post marks the end of the window.
                    reached_older |= not _stickied(child)
                    continue
                yield item
            if reached_older or after is None:
                return
        yield Rejection(
            f"reddit:r/{subreddit}",
            f"page cap of {self._params.max_pages_per_subreddit} reached before the "
            "window start; older posts were not read",
        )


def _listing(response: Any) -> tuple[list[Any], str | None]:
    try:
        body = response.json()
    except ValueError:
        raise MalformedResponse("Reddit listing is not JSON") from None
    data = body.get("data") if isinstance(body, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(children, list):
        raise MalformedResponse("Reddit listing has no data.children list")
    after = data.get("after")
    return children, after if isinstance(after, str) and after else None


def _stickied(child: Any) -> bool:
    return bool(isinstance(child, dict) and (child.get("data") or {}).get("stickied"))


def _web_url(value: Any) -> str | None:
    if isinstance(value, str) and value.lower().startswith(("https://", "http://")):
        return value
    return None


def _to_item(child: Any, subreddit: str) -> FetchedItem | Rejection:
    data = child.get("data") if isinstance(child, dict) else None
    if not isinstance(child, dict) or child.get("kind") != "t3" or not isinstance(data, dict):
        return Rejection(f"reddit:r/{subreddit}", "listing child is not a post")
    name, created = data.get("name"), data.get("created_utc")
    if not isinstance(name, str) or not name.startswith("t3_"):
        return Rejection(f"reddit:r/{subreddit}", "post without a t3_ fullname")
    if not isinstance(created, int | float) or isinstance(created, bool):
        return Rejection(f"reddit:{name}", "post without created_utc")
    sub = data.get("subreddit")
    if isinstance(sub, str) and sub.lower().startswith("u_"):
        # A post on someone's profile page: the "subreddit" is their name.
        return Rejection(f"reddit:{name}", "post on a user profile, not a subreddit")
    try:
        posted_at = datetime.fromtimestamp(created, UTC)
    except (ValueError, OverflowError, OSError):
        return Rejection(f"reddit:{name}", "created_utc out of range")
    permalink = data.get("permalink")
    discussion = f"{SITE}{permalink}" if isinstance(permalink, str) else None
    link = _web_url(data.get("url")) if not data.get("is_self") else None
    score, comments = data.get("score"), data.get("num_comments")
    author, title, body = data.get("author"), data.get("title"), data.get("selftext")
    return FetchedItem(
        external_id=name,
        title=title if isinstance(title, str) else None,
        text=body if isinstance(body, str) else "",
        # Link posts keep their link so the same article dedupes across sources.
        url=link or discussion,
        author=author if isinstance(author, str) and author != "[deleted]" else None,
        points=score if isinstance(score, int) and score >= 0 else None,
        num_comments=comments if isinstance(comments, int) and comments >= 0 else None,
        posted_at=posted_at,
        raw={k: data[k] for k in _RAW_FIELDS if k in data},
    )
