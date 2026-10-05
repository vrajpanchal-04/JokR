"""Migration 0002: Scout's tables, their constraints, and who may touch them.

Constraints live in the database so a bug in Scout cannot store a malformed
row, and grants make signals insert-only for every app role.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import DBAPIError

HEX64 = "a" * 64


def _source(db: Connection, name: str = "hackernews", kind: str = "api") -> int:
    api = kind == "api"
    return int(
        db.execute(
            text(
                """
                INSERT INTO sources (name, type, enabled, tos_url, allowed_hosts,
                                     min_interval_s, max_requests)
                VALUES (:name, :type, true, :tos, :hosts, :interval, :max)
                RETURNING id
                """
            ),
            {
                "name": name,
                "type": kind,
                "tos": "https://hn.algolia.com/api" if api else None,
                "hosts": ["hn.algolia.com"] if api else [],
                "interval": 1.0 if api else None,
                "max": 60 if api else None,
            },
        ).scalar_one()
    )


def _run(db: Connection, source_id: int) -> int:
    return int(
        db.execute(
            text("INSERT INTO runs (agent, source_id) VALUES ('scout', :s) RETURNING id"),
            {"s": source_id},
        ).scalar_one()
    )


def _signal_params(source_id: int, run_id: int, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source_id": source_id,
        "run_id": run_id,
        "external_id": "123",
        "url": "https://news.ycombinator.com/item?id=123",
        "url_canonical": "news.ycombinator.com/item?id=123",
        "locator": None,
        "title": "We built our own internal tool for payroll",
        "text": "budget for a tool",
        "author_hash": HEX64,
        "content_hash": HEX64,
        "raw": "{}",
        "intent_score": 1.5,
        "intent_terms": ["budget for"],
        "intent_version": "1",
    }
    values.update(overrides)
    return values


INSERT_SIGNAL = text(
    """
    INSERT INTO signals (source_id, run_id, external_id, url, url_canonical, locator, title,
                         text, author_hash, content_hash, raw, intent_score, intent_terms,
                         intent_version)
    VALUES (:source_id, :run_id, :external_id, :url, :url_canonical, :locator, :title,
            :text, :author_hash, :content_hash, CAST(:raw AS jsonb), :intent_score,
            :intent_terms, :intent_version)
    ON CONFLICT (source_id, external_id) DO NOTHING
    RETURNING id
    """
)


@pytest.fixture
def ids(db: Connection) -> Iterator[tuple[int, int]]:
    source_id = _source(db)
    yield source_id, _run(db, source_id)


def _fails(db: Connection, sql: str, params: dict[str, Any] | None = None) -> None:
    savepoint = db.begin_nested()
    with pytest.raises(DBAPIError):
        db.execute(text(sql), params or {})
    savepoint.rollback()


def _fails_insert(db: Connection, params: dict[str, Any]) -> None:
    savepoint = db.begin_nested()
    with pytest.raises(DBAPIError):
        db.execute(INSERT_SIGNAL, params)
    savepoint.rollback()


# --- schema -------------------------------------------------------------------


def test_scout_tables_exist(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert {"sources", "signals", "runs"} <= tables


def test_models_and_migrations_do_not_drift(engine: Engine, migration_config: Config) -> None:
    command.check(migration_config)


def test_downgrade_to_0001_and_back(engine: Engine, migration_config: Config) -> None:
    command.downgrade(migration_config, "0001")
    tables = set(inspect(engine).get_table_names())
    assert "signals" not in tables
    assert "decisions_log" in tables
    command.upgrade(migration_config, "head")
    assert "signals" in inspect(engine).get_table_names()


def test_signal_insert_is_idempotent(db: Connection, ids: tuple[int, int]) -> None:
    first = db.execute(INSERT_SIGNAL, _signal_params(*ids)).scalar_one_or_none()
    second = db.execute(INSERT_SIGNAL, _signal_params(*ids)).scalar_one_or_none()
    assert first is not None
    assert second is None


def test_signal_defaults(db: Connection, ids: tuple[int, int]) -> None:
    row_id = db.execute(INSERT_SIGNAL, _signal_params(*ids)).scalar_one()
    row = db.execute(
        text("SELECT trust, flags, fetched_at FROM signals WHERE id = :id"), {"id": row_id}
    ).one()
    assert row.trust == "untrusted"
    assert row.flags == []
    assert row.fetched_at is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"external_id": ""},
        {"external_id": "x" * 513},
        {"author_hash": "not-a-hash"},
        {"content_hash": "A" * 64},
        {"text": "x" * 32_769},
        {"url": "https://x/" + "a" * 2048},
        {"url": None, "locator": None},
        {"intent_score": -1},
        {"intent_version": ""},
        {"raw": '"just a string"'},
    ],
    ids=[
        "empty-external-id",
        "long-external-id",
        "bad-author-hash",
        "uppercase-content-hash",
        "text-over-32k",
        "url-over-2048",
        "no-url-or-locator",
        "negative-intent",
        "empty-intent-version",
        "raw-not-object",
    ],
)
def test_signal_constraints(
    db: Connection, ids: tuple[int, int], overrides: dict[str, Any]
) -> None:
    _fails_insert(db, _signal_params(*ids, **overrides))


def test_api_source_needs_tos_and_limits(db: Connection) -> None:
    _fails(
        db,
        "INSERT INTO sources (name, type, enabled) VALUES ('hn2', 'api', true)",
    )


@pytest.mark.parametrize("name", ["HN", "1hn", "hn-news", ""])
def test_source_name_is_checked(db: Connection, name: str) -> None:
    _fails(
        db,
        "INSERT INTO sources (name, type, enabled) VALUES (:n, 'inbox', true)",
        {"n": name},
    )


def test_source_with_signals_cannot_be_deleted(db: Connection, ids: tuple[int, int]) -> None:
    db.execute(INSERT_SIGNAL, _signal_params(*ids))
    _fails(db, "DELETE FROM sources WHERE id = :id", {"id": ids[0]})


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE runs SET status = 'done' WHERE id = :id",
        "UPDATE runs SET status = 'ok', finished_at = started_at - interval '1 second' "
        "WHERE id = :id",
        "UPDATE runs SET status = 'ok' WHERE id = :id",  # finished runs need finished_at
        "UPDATE runs SET n_new = -1 WHERE id = :id",
        "UPDATE runs SET rejections = '{}'::jsonb WHERE id = :id",
    ],
    ids=["bad-status", "finish-before-start", "ok-without-finish", "negative-count", "rej-obj"],
)
def test_run_constraints(db: Connection, ids: tuple[int, int], sql: str) -> None:
    _fails(db, sql, {"id": ids[1]})


def test_run_can_finish(db: Connection, ids: tuple[int, int]) -> None:
    db.execute(
        text(
            "UPDATE runs SET status = 'partial', finished_at = now(), n_fetched = 5, "
            'rejections = \'[{"locator": "a.csv:3", "reason": "bad"}]\' WHERE id = :id'
        ),
        {"id": ids[1]},
    )


# --- roles --------------------------------------------------------------------


def test_scout_role_can_do_its_job(db: Connection) -> None:
    source_id = _source(db)
    db.execute(text("SET LOCAL ROLE jokr_scout"))
    assert db.execute(text("SELECT count(*) FROM sources")).scalar_one() >= 1
    run_id = _run(db, source_id)
    assert db.execute(INSERT_SIGNAL, _signal_params(source_id, run_id)).scalar_one()
    db.execute(
        text(
            "UPDATE runs SET status = 'ok', finished_at = now(), n_fetched = 1, n_new = 1, "
            "n_skipped = 0, error = NULL, rejections = '[]' WHERE id = :id"
        ),
        {"id": run_id},
    )
    log_id = db.execute(
        text(
            "INSERT INTO decisions_log (agent, action, reason) "
            "VALUES ('scout', 'run', 'test') RETURNING id"
        )
    ).scalar_one()
    assert log_id


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE signals SET text = 'rewritten'",
        "DELETE FROM signals",
        "TRUNCATE signals",
        "UPDATE sources SET enabled = false",
        "INSERT INTO sources (name, type, enabled) VALUES ('rogue', 'inbox', true)",
        "DELETE FROM sources",
        "UPDATE runs SET agent = 'other'",
        "UPDATE runs SET started_at = now()",
        "DELETE FROM runs",
        "UPDATE decisions_log SET reason = 'x'",
        "SELECT reason FROM decisions_log",
        "ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_text_size",
    ],
)
def test_scout_role_limits(db: Connection, ids: tuple[int, int], sql: str) -> None:
    db.execute(INSERT_SIGNAL, _signal_params(*ids))
    db.execute(text("SET LOCAL ROLE jokr_scout"))
    _fails(db, sql)


def test_app_role_reads_scout_tables(db: Connection, ids: tuple[int, int]) -> None:
    db.execute(INSERT_SIGNAL, _signal_params(*ids))
    db.execute(text("SET LOCAL ROLE jokr_app"))
    for table in ("sources", "signals", "runs"):
        assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() >= 1  # noqa: S608


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO runs (agent, source_id) SELECT 'api', id FROM sources LIMIT 1",
        "UPDATE runs SET status = 'failed'",
        "DELETE FROM signals",
        "UPDATE sources SET enabled = false",
    ],
)
def test_app_role_cannot_write_scout_tables(db: Connection, ids: tuple[int, int], sql: str) -> None:
    db.execute(text("SET LOCAL ROLE jokr_app"))
    _fails(db, sql)


def test_app_role_cannot_insert_signals(db: Connection, ids: tuple[int, int]) -> None:
    db.execute(text("SET LOCAL ROLE jokr_app"))
    _fails_insert(db, _signal_params(*ids))
