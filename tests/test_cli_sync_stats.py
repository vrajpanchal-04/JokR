"""sources sync, scout stats and the jokr CLI wiring."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from jokr.agents.scout_stats import scout_stats
from jokr.agents.sources_sync import sync_sources
from jokr.cli import build_parser, format_stats, main, make_open_connector
from jokr.connectors.hn import HackerNewsConnector
from jokr.connectors.inbox import InboxConnector
from jokr.settings import ScoutSettings
from tests.guard_helpers import api_source

HN = api_source("hackernews")
ARXIV = api_source("arxiv", ("export.arxiv.org",))


@pytest.fixture
async def owner_async(committed_db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(committed_db_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def owner(committed_db_url: str) -> Iterator[Any]:
    eng = create_engine(committed_db_url)
    yield eng
    eng.dispose()


def _sources(owner: Any) -> dict[str, Any]:
    with owner.connect() as conn:
        return {r.name: r for r in conn.execute(text("SELECT * FROM sources"))}


async def test_sync_inserts_updates_retires_and_revives(
    owner_async: AsyncEngine, owner: Any
) -> None:
    result = await sync_sources(owner_async, [HN, ARXIV])
    assert result.upserted == ("arxiv", "hackernews")
    rows = _sources(owner)
    assert rows["hackernews"].allowed_hosts == ["hn.algolia.com"]
    assert rows["hackernews"].retired_at is None

    slower = HN.model_copy(update={"min_interval_s": 2.5})
    result = await sync_sources(owner_async, [slower])
    assert result.retired == ("arxiv",)
    rows = _sources(owner)
    assert rows["hackernews"].min_interval_s == 2.5
    assert rows["arxiv"].retired_at is not None and rows["arxiv"].enabled is False

    await sync_sources(owner_async, [HN, ARXIV])
    assert _sources(owner)["arxiv"].retired_at is None


async def test_sync_is_refused_for_the_scout_role(committed_db_url: str) -> None:
    scout = create_async_engine(committed_db_url, connect_args={"options": "-c role=jokr_scout"})
    try:
        with pytest.raises(Exception, match="permission denied"):
            await sync_sources(scout, [HN])
    finally:
        await scout.dispose()


async def test_stats_rank_by_sql_and_find_recurring_pain(
    owner_async: AsyncEngine, owner: Any
) -> None:
    await sync_sources(owner_async, [HN, ARXIV])
    with owner.begin() as conn:
        ids = {r.name: r.id for r in conn.execute(text("SELECT id, name FROM sources"))}
        runs = {
            name: conn.execute(
                text("INSERT INTO runs (agent, source_id) VALUES ('scout', :s) RETURNING id"),
                {"s": sid},
            ).scalar_one()
            for name, sid in ids.items()
        }
        rows = [
            # (source, ext, score, points, comments, content_hash, posted day)
            ("hackernews", "s1", 2.25, 10, 5, "a" * 64, 1),
            ("hackernews", "s2", 2.25, 50, 1, "b" * 64, 2),
            ("hackernews", "s3", 1.0, 999, 99, "c" * 64, 3),
            ("arxiv", "s4", 1.5, None, None, "a" * 64, 4),
        ]
        for name, ext, score, pts, com, chash, day in rows:
            conn.execute(
                text(
                    "INSERT INTO signals (source_id, run_id, external_id, url, text, content_hash, "
                    "raw, intent_score, intent_terms, intent_version, points, num_comments, "
                    "posted_at, title) VALUES (:s, :r, :e, :u, 'x', :h, '{}', :sc, '{}', 't', "
                    ":p, :c, :at, :t)"
                ),
                {
                    "s": ids[name],
                    "r": runs[name],
                    "e": f"stats-{ext}",
                    "u": f"https://example.com/{ext}",
                    "h": chash,
                    "sc": score,
                    "p": pts,
                    "c": com,
                    "at": datetime(2026, 9, day, tzinfo=UTC),
                    "t": f"title {ext}",
                },
            )
    stats = await scout_stats(owner_async, top_n=3)
    assert [t.title for t in stats.top] == ["title s2", "title s1", "title s4"]
    recurring = [r for r in stats.recurring if r.kind == "content" and r.key == "a" * 64]
    assert recurring and (recurring[0].days, recurring[0].sources, recurring[0].signals) == (
        2,
        2,
        2,
    )
    assert {s.name for s in stats.per_source} >= {"hackernews", "arxiv"}
    report = format_stats(stats)
    assert "title s2" in report and "Recurring" in report


def test_parser_validates_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["scout", "run", "--source", "arxiv"]).source == "arxiv"
    assert parser.parse_args(["scout", "stats"]).top == 20
    for bad in (["scout"], ["scout", "stats", "--top", "0"], ["nope"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


def test_config_error_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1:1/none")
    monkeypatch.setenv("JOKR_CONFIG_DIR", str(tmp_path))
    assert main(["sources", "sync"]) == 1


def _settings(**overrides: Any) -> ScoutSettings:
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://x:y@db/jokr",
        "author_hash_salt": "s" * 32,
        "inbox_root": "data/inbox",
    }
    values.update(overrides)
    return ScoutSettings.model_validate(values)


async def test_open_connector_builds_the_right_connector() -> None:
    from jokr.config import load_config

    cfg = load_config(Path(__file__).resolve().parents[1] / "config")
    inbox = cfg.sources.get("inbox")
    assert inbox is not None
    open_connector = make_open_connector(_settings())
    async with open_connector(HN) as connector:
        assert isinstance(connector, HackerNewsConnector)
    async with open_connector(inbox) as connector:
        assert isinstance(connector, InboxConnector)


async def test_reddit_without_credentials_fails_loudly() -> None:
    reddit = api_source("reddit", ("www.reddit.com", "oauth.reddit.com"))
    with pytest.raises(RuntimeError, match="credentials"):
        async with make_open_connector(_settings())(reddit):
            pass
