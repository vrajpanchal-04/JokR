"""`jokr scout stats`: numbers computed by SQL from logged rows only (C5)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_PER_SOURCE = text(
    """
    SELECT so.name, count(s.id) AS signals, max(s.fetched_at) AS last_fetched,
           (SELECT r.status FROM runs r WHERE r.source_id = so.id
             ORDER BY r.started_at DESC LIMIT 1) AS last_status
    FROM sources so LEFT JOIN signals s ON s.source_id = so.id
    WHERE so.retired_at IS NULL
    GROUP BY so.id, so.name ORDER BY so.name
    """
)
_TOP = text(
    """
    SELECT s.id, so.name AS source, s.title, s.url, s.intent_score, s.intent_terms,
           s.points, s.num_comments
    FROM signals s JOIN sources so ON so.id = s.source_id
    ORDER BY s.intent_score DESC, s.points DESC NULLS LAST, s.num_comments DESC NULLS LAST, s.id
    LIMIT :n
    """
)
# Days are counted in UTC so the answer does not depend on the server timezone.
_RECURRING = text(
    """
    WITH d AS (
        SELECT s.content_hash, s.url_canonical, s.source_id, s.title,
               (COALESCE(s.posted_at, s.fetched_at) AT TIME ZONE 'UTC')::date AS day
        FROM signals s
    ), g AS (
        SELECT 'content' AS kind, content_hash AS key, min(title) AS sample_title,
               count(DISTINCT day) AS days, count(DISTINCT source_id) AS sources,
               count(*) AS signals
        FROM d GROUP BY content_hash
        UNION ALL
        SELECT 'url', url_canonical, min(title),
               count(DISTINCT day), count(DISTINCT source_id), count(*)
        FROM d WHERE url_canonical IS NOT NULL GROUP BY url_canonical
    )
    SELECT kind, key, sample_title, days, sources, signals FROM g
    WHERE days > 1 OR sources > 1
    ORDER BY days DESC, sources DESC, signals DESC, key
    LIMIT :n
    """
)


@dataclass(frozen=True)
class SourceStats:
    name: str
    signals: int
    last_fetched: datetime | None
    last_status: str | None


@dataclass(frozen=True)
class TopSignal:
    id: int
    source: str
    title: str | None
    url: str | None
    intent_score: Decimal
    intent_terms: list[str]
    points: int | None
    num_comments: int | None


@dataclass(frozen=True)
class Recurring:
    kind: str
    key: str
    sample_title: str | None
    days: int
    sources: int
    signals: int


@dataclass(frozen=True)
class Stats:
    per_source: list[SourceStats]
    top: list[TopSignal]
    recurring: list[Recurring]


async def scout_stats(engine: AsyncEngine, top_n: int = 20) -> Stats:
    async with engine.connect() as conn:
        per_source = [SourceStats(*r) for r in await conn.execute(_PER_SOURCE)]
        top = [TopSignal(*r) for r in await conn.execute(_TOP, {"n": top_n})]
        recurring = [Recurring(*r) for r in await conn.execute(_RECURRING, {"n": top_n})]
    return Stats(per_source, top, recurring)
