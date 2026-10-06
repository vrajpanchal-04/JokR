"""Scout run: per-source isolation, honest counts, logging and exit codes (B4-B6)."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from jokr.agents.scout import ScoutDeps, run_scout
from jokr.config import IntentLexicon, Source
from jokr.connectors.base import Connector, FetchedItem, Rejection
from jokr.guards.ratelimit import BudgetExhausted
from jokr.guards.tos import HostNotAllowed
from tests.guard_helpers import api_source

SALT = b"s" * 32
LEX = IntentLexicon.model_validate(
    {"version": "t1", "cap": 5, "groups": [{"name": "money", "weight": 1.5, "terms": ["payroll"]}]}
)
HN = api_source("hackernews")
ARXIV = api_source("arxiv", ("export.arxiv.org",))


@pytest.fixture(scope="module")
def seeded(committed_db_url: str) -> str:
    """Mirror the two test sources into `sources`, as `jokr sources sync` would."""
    eng = create_engine(committed_db_url)
    with eng.begin() as conn:
        for s in (HN, ARXIV):
            conn.execute(
                text(
                    "INSERT INTO sources (name, type, enabled, tos_url, allowed_hosts, "
                    "min_interval_s, max_requests) VALUES (:n, 'api', true, :t, :h, 0, 50)"
                ),
                {"n": s.name, "t": str(s.tos_url), "h": list(s.allowed_hosts)},
            )
    eng.dispose()
    return committed_db_url


@pytest.fixture
async def scout_engine(seeded: str) -> AsyncIterator[AsyncEngine]:
    """Connects as jokr_scout, so the run proves it works within that role's grants."""
    eng = create_async_engine(seeded, connect_args={"options": "-c role=jokr_scout"})
    yield eng
    await eng.dispose()


@pytest.fixture
def owner(seeded: str) -> Iterator[Any]:
    eng = create_engine(seeded)
    yield eng
    eng.dispose()


class Fake:
    def __init__(
        self,
        items: Sequence[FetchedItem | Rejection] = (),
        *,
        error: BaseException | None = None,
        delay: float = 0,
    ) -> None:
        self.items = items
        self.error = error
        self.delay = delay
        self.since: list[datetime | None] = []

    async def fetch(self, since: datetime | None) -> AsyncIterator[FetchedItem | Rejection]:
        self.since.append(since)
        for item in self.items:
            yield item
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error


def _deps(fakes: dict[str, Fake]) -> ScoutDeps:
    @asynccontextmanager
    async def open_connector(source: Source) -> AsyncIterator[Connector]:
        yield fakes[source.name]

    return ScoutDeps(salt=SALT, lexicon=LEX, open_connector=open_connector)


def _item(n: int | str, **overrides: Any) -> FetchedItem:
    values: dict[str, Any] = {
        "external_id": f"{n}",
        "title": f"Payroll pain {n}",
        "text": "our payroll breaks",
        "url": f"https://example.com/{n}",
        "author": "someone",
        "posted_at": datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=int(str(n)[-4:], 16)),
    }
    values.update(overrides)
    return FetchedItem(**values)


def _ids(k: int) -> list[str]:
    tag = uuid4().hex[:8]
    return [f"{tag}{i:04x}" for i in range(k)]


def _run_row(owner: Any, run_id: int | None) -> Any:
    with owner.connect() as conn:
        return conn.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": run_id}).one()


def _last_log(owner: Any) -> Any:
    with owner.connect() as conn:
        return conn.execute(text("SELECT * FROM decisions_log ORDER BY id DESC LIMIT 1")).one()


async def test_happy_path_stores_and_logs(scout_engine: AsyncEngine, owner: Any) -> None:
    ids = _ids(3)
    fakes = {"hackernews": Fake([_item(i) for i in ids]), "arxiv": Fake([_item(_ids(1)[0])])}
    result = await run_scout(scout_engine, [HN, ARXIV], _deps(fakes))
    assert (result.status, result.exit_code) == ("ok", 0)
    hn = result.outcomes[0]
    assert (hn.n_fetched, hn.n_new, hn.n_skipped) == (3, 3, 0)
    row = _run_row(owner, hn.run_id)
    assert (row.status, row.n_new, row.agent) == ("ok", 3, "scout")
    assert row.finished_at is not None
    log = _last_log(owner)
    assert log.action == "scout_run_ok"
    assert set(log.evidence_ids) == {o.run_id for o in result.outcomes}
    with owner.connect() as conn:
        stored = conn.execute(
            text("SELECT trust, intent_score, author_hash FROM signals WHERE external_id = :e"),
            {"e": ids[0]},
        ).one()
    assert stored.trust == "untrusted"
    assert stored.intent_score > 1
    assert stored.author_hash and "someone" not in stored.author_hash


async def test_second_run_is_idempotent(scout_engine: AsyncEngine) -> None:
    items = [_item(i) for i in _ids(4)]
    deps = _deps({"hackernews": Fake(items)})
    first = await run_scout(scout_engine, [HN], deps)
    second = await run_scout(scout_engine, [HN], deps)
    assert first.outcomes[0].n_new == 4
    assert (second.outcomes[0].n_fetched, second.outcomes[0].n_new) == (4, 0)


async def test_watermark_is_the_start_of_the_last_ok_run(
    scout_engine: AsyncEngine, owner: Any
) -> None:
    # A far-future post must not drag the watermark forward and hide real items.
    future = datetime(2030, 1, 1, tzinfo=UTC)
    first = await run_scout(
        scout_engine, [HN], _deps({"hackernews": Fake([_item(_ids(1)[0], posted_at=future)])})
    )
    started = _run_row(owner, first.outcomes[0].run_id).started_at
    fake = Fake()
    await run_scout(scout_engine, [HN], _deps({"hackernews": fake}))
    assert fake.since == [started]


async def test_unfinished_runs_do_not_move_the_watermark(
    scout_engine: AsyncEngine, owner: Any
) -> None:
    ok = await run_scout(scout_engine, [HN], _deps({"hackernews": Fake()}))
    started = _run_row(owner, ok.outcomes[0].run_id).started_at
    # A budget-capped run stored only its newest items, so older ones were never read.
    capped = Fake([_item(_ids(1)[0])], error=BudgetExhausted("max_requests=1 reached"))
    await run_scout(scout_engine, [HN], _deps({"hackernews": capped}))
    await run_scout(scout_engine, [HN], _deps({"hackernews": Fake(error=RuntimeError("x"))}))
    fake = Fake()
    await run_scout(scout_engine, [HN], _deps({"hackernews": fake}))
    assert fake.since == [started]


async def test_repeats_within_a_run_are_not_counted_as_fetched(
    scout_engine: AsyncEngine,
) -> None:
    item = _item(_ids(1)[0])
    result = await run_scout(scout_engine, [HN], _deps({"hackernews": Fake([item, item])}))
    outcome = result.outcomes[0]
    assert (outcome.n_fetched, outcome.n_new, outcome.n_skipped) == (1, 1, 0)


async def test_rejection_locators_are_scrubbed(scout_engine: AsyncEngine, owner: Any) -> None:
    bad = Rejection("https://www.reddit.com/user/janedoe/x", "bad")
    result = await run_scout(scout_engine, [HN], _deps({"hackernews": Fake([bad])}))
    row = _run_row(owner, result.outcomes[0].run_id)
    assert "janedoe" not in str(row.rejections)


async def test_failed_final_store_keeps_the_original_error(
    scout_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jokr.agents import scout

    async def broken_store(*args: Any) -> int:
        raise RuntimeError("database went away")

    monkeypatch.setattr(scout, "_store", broken_store)
    fake = Fake([_item(_ids(1)[0])], error=RuntimeError("source broke"))
    result = await run_scout(scout_engine, [HN], _deps({"hackernews": fake}))
    error = result.outcomes[0].error or ""
    assert "source broke" in error
    assert "1 fetched item(s) could not be stored" in error


async def test_unexpected_errors_say_where_they_happened(scout_engine: AsyncEngine) -> None:
    fake = Fake(error=KeyError("objectID"))
    result = await run_scout(scout_engine, [HN], _deps({"hackernews": fake}))
    assert "test_scout_run.py:" in (result.outcomes[0].error or "")


async def test_one_broken_source_does_not_stop_the_other(
    scout_engine: AsyncEngine, owner: Any, caplog: pytest.LogCaptureFixture
) -> None:
    fakes = {
        "hackernews": Fake([_item(_ids(1)[0])], error=RuntimeError("boom Bearer abc123secret")),
        "arxiv": Fake([_item(_ids(1)[0])]),
    }
    result = await run_scout(scout_engine, [HN, ARXIV], _deps(fakes))
    assert (result.status, result.exit_code) == ("partial", 0)
    hn, arxiv = result.outcomes
    assert hn.status == "failed" and hn.n_new == 1  # stored before the failure
    assert "abc123secret" not in (hn.error or "")
    assert "abc123secret" not in caplog.text
    assert _run_row(owner, hn.run_id).status == "failed"
    assert arxiv.status == "ok" and arxiv.n_new == 1


async def test_all_sources_failing_exits_non_zero(scout_engine: AsyncEngine) -> None:
    fakes = {"hackernews": Fake(error=RuntimeError("x")), "arxiv": Fake(error=RuntimeError("y"))}
    result = await run_scout(scout_engine, [HN, ARXIV], _deps(fakes))
    assert (result.status, result.exit_code) == ("failed", 1)


async def test_c3_violation_is_logged_and_exits_two(scout_engine: AsyncEngine, owner: Any) -> None:
    fakes = {"hackernews": Fake(error=HostNotAllowed("host 'evil.com' is not on the allowlist"))}
    result = await run_scout(scout_engine, [HN], _deps(fakes))
    assert result.exit_code == 2
    with owner.connect() as conn:
        actions = conn.execute(
            text("SELECT action, reason FROM decisions_log ORDER BY id DESC LIMIT 2")
        ).all()
    assert actions[1].action == "c3_violation"
    assert "evil.com" in actions[1].reason


async def test_budget_cap_keeps_what_was_fetched(scout_engine: AsyncEngine) -> None:
    fake = Fake([_item(i) for i in _ids(2)], error=BudgetExhausted("max_requests=2 reached"))
    result = await run_scout(scout_engine, [HN], _deps({"hackernews": fake}))
    outcome = result.outcomes[0]
    assert (outcome.status, outcome.n_new) == ("partial", 2)
    assert result.exit_code == 0


async def test_time_limit_stops_a_slow_source(scout_engine: AsyncEngine) -> None:
    slow = HN.model_copy(update={"max_runtime_s": 1})
    fake = Fake([_item(_ids(1)[0])], delay=5)
    result = await run_scout(scout_engine, [slow], _deps({"hackernews": fake}))
    outcome = result.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.error == "time limit of 1s reached"
    assert outcome.n_new == 1  # fetched before the limit, so kept


async def test_rejections_are_counted_and_capped(scout_engine: AsyncEngine, owner: Any) -> None:
    rejected: list[FetchedItem | Rejection] = [Rejection(f"f.csv:{n}", "bad") for n in range(150)]
    bad_item = _item(_ids(1)[0], url=None, locator=None)
    result = await run_scout(scout_engine, [HN], _deps({"hackernews": Fake([*rejected, bad_item])}))
    outcome = result.outcomes[0]
    assert outcome.n_skipped == 151
    row = _run_row(owner, outcome.run_id)
    assert len(row.rejections) == 100
    assert row.rejections[-1]["reason"] == "52 more rejections not listed"
    assert row.n_skipped == 151


async def test_locked_source_is_skipped_and_recorded(
    scout_engine: AsyncEngine, seeded: str
) -> None:
    holder = create_engine(seeded)
    with holder.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(hashtext('jokr.scout.hackernews'))"))
        result = await run_scout(scout_engine, [HN], _deps({"hackernews": Fake()}))
        conn.execute(text("SELECT pg_advisory_unlock(hashtext('jokr.scout.hackernews'))"))
    holder.dispose()
    assert result.outcomes[0].status == "failed"
    assert "another Scout run" in (result.outcomes[0].error or "")


async def test_unsynced_source_fails_clearly(scout_engine: AsyncEngine) -> None:
    reddit = api_source("reddit", ("www.reddit.com", "oauth.reddit.com"))
    result = await run_scout(scout_engine, [reddit], _deps({"reddit": Fake()}))
    assert "sources sync" in (result.outcomes[0].error or "")
    assert result.exit_code == 1


async def test_only_and_disabled_filters(scout_engine: AsyncEngine) -> None:
    off = ARXIV.model_copy(update={"enabled": False})
    fakes = {"hackernews": Fake(), "arxiv": Fake()}
    result = await run_scout(scout_engine, [HN, off], _deps(fakes))
    assert [o.name for o in result.outcomes] == ["hackernews"]
    result = await run_scout(scout_engine, [HN, ARXIV], _deps(fakes), only="arxiv")
    assert [o.name for o in result.outcomes] == ["arxiv"]


async def test_no_enabled_sources_is_an_error(scout_engine: AsyncEngine, owner: Any) -> None:
    result = await run_scout(scout_engine, [], _deps({}))
    assert result.exit_code == 1
    assert _last_log(owner).reason == "no enabled sources"
