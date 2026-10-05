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


# SQLSTATEs, so a typo in a test statement can't pass as a refusal.
DENIED = "42501"  # insufficient_privilege (grants, ownership, our triggers)
CHECK = "23514"  # check_violation
FK = "23503"  # foreign_key_violation


def _fails(db: Connection, sql: Any, params: dict[str, Any] | None = None, *, code: str) -> None:
    savepoint = db.begin_nested()
    with pytest.raises(DBAPIError) as info:
        db.execute(text(sql) if isinstance(sql, str) else sql, params or {})
    savepoint.rollback()
    assert getattr(info.value.orig, "sqlstate", None) == code, info.value


def _fails_insert(db: Connection, params: dict[str, Any], *, code: str = CHECK) -> None:
    _fails(db, INSERT_SIGNAL, params, code=code)


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
        {"intent_score": "NaN"},
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
        "nan-intent",
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
        code=CHECK,
    )


@pytest.mark.parametrize("name", ["HN", "1hn", "hn-news", ""])
def test_source_name_is_checked(db: Connection, name: str) -> None:
    _fails(
        db,
        "INSERT INTO sources (name, type, enabled) VALUES (:n, 'inbox', true)",
        {"n": name},
        code=CHECK,
    )


def test_source_with_signals_cannot_be_deleted(db: Connection, ids: tuple[int, int]) -> None:
    db.execute(INSERT_SIGNAL, _signal_params(*ids))
    _fails(db, "DELETE FROM sources WHERE id = :id", {"id": ids[0]}, code=FK)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE runs SET status = 'done' WHERE id = :id",
        "UPDATE runs SET status = 'ok', finished_at = started_at - interval '1 second' "
        "WHERE id = :id",
        "UPDATE runs SET status = 'ok' WHERE id = :id",  # finished runs need finished_at
        "UPDATE runs SET n_new = -1 WHERE id = :id",
        "UPDATE runs SET rejections = '{}'::jsonb WHERE id = :id",
        "UPDATE runs SET cost = 'NaN' WHERE id = :id",
    ],
    ids=[
        "bad-status",
        "finish-before-start",
        "ok-without-finish",
        "negative-count",
        "rej-obj",
        "nan-cost",
    ],
)
def test_run_constraints(db: Connection, ids: tuple[int, int], sql: str) -> None:
    _fails(db, sql, {"id": ids[1]}, code=CHECK)


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
        "UPDATE runs SET source_id = source_id",
        "UPDATE runs SET tokens = 1",
        "UPDATE runs SET cost = 1",
        "INSERT INTO runs (agent, source_id, status, finished_at) "
        "SELECT 'scout', id, 'ok', now() FROM sources LIMIT 1",
        "INSERT INTO runs (agent, source_id, n_new) SELECT 'scout', id, 99 FROM sources LIMIT 1",
        "CREATE TABLE rogue (x int)",
    ],
)
def test_scout_role_limits(db: Connection, ids: tuple[int, int], sql: str) -> None:
    db.execute(INSERT_SIGNAL, _signal_params(*ids))
    db.execute(text("SET LOCAL ROLE jokr_scout"))
    _fails(db, sql, code=DENIED)


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
        "UPDATE signals SET text = 'x'",
        "TRUNCATE signals",
        "INSERT INTO sources (name, type, enabled) VALUES ('rogue', 'inbox', true)",
        "UPDATE decisions_log SET reason = 'x'",
        "CREATE TABLE rogue (x int)",
    ],
)
def test_app_role_cannot_write_scout_tables(db: Connection, ids: tuple[int, int], sql: str) -> None:
    db.execute(text("SET LOCAL ROLE jokr_app"))
    _fails(db, sql, code=DENIED)


def test_app_role_cannot_insert_signals(db: Connection, ids: tuple[int, int]) -> None:
    db.execute(text("SET LOCAL ROLE jokr_app"))
    _fails_insert(db, _signal_params(*ids), code=DENIED)


def test_scout_conflict_path_returns_nothing(db: Connection, ids: tuple[int, int]) -> None:
    db.execute(text("SET LOCAL ROLE jokr_scout"))
    assert db.execute(INSERT_SIGNAL, _signal_params(*ids)).scalar_one()
    assert db.execute(INSERT_SIGNAL, _signal_params(*ids)).scalar_one_or_none() is None


def test_signal_must_belong_to_a_run_of_its_own_source(db: Connection) -> None:
    hn = _source(db, "hackernews")
    inbox = _source(db, "inbox", kind="inbox")
    inbox_run = _run(db, inbox)
    _fails_insert(db, _signal_params(hn, inbox_run), code=FK)


def test_finished_run_cannot_be_reopened_or_rewritten(db: Connection, ids: tuple[int, int]) -> None:
    run_id = ids[1]
    db.execute(
        text("UPDATE runs SET status = 'ok', finished_at = now() WHERE id = :id"), {"id": run_id}
    )
    db.execute(text("SET LOCAL ROLE jokr_scout"))
    for sql in (
        "UPDATE runs SET status = 'running', finished_at = NULL WHERE id = :id",
        "UPDATE runs SET n_new = 999 WHERE id = :id",
    ):
        _fails(db, sql, {"id": run_id}, code=DENIED)


def test_owner_cannot_edit_signals_either(db: Connection, ids: tuple[int, int]) -> None:
    """Defense in depth: the insert-only trigger holds even for the table owner."""
    db.execute(INSERT_SIGNAL, _signal_params(*ids))
    for sql in ("UPDATE signals SET text = 'x'", "DELETE FROM signals", "TRUNCATE signals"):
        _fails(db, sql, code=DENIED)


def test_new_run_takes_its_defaults(db: Connection) -> None:
    source_id = _source(db)
    db.execute(text("SET LOCAL ROLE jokr_scout"))
    row = db.execute(
        text("SELECT status, finished_at, n_new FROM runs WHERE id = :id"),
        {"id": _run(db, source_id)},
    ).one()
    assert (row.status, row.finished_at, row.n_new) == ("running", None, 0)


@pytest.mark.parametrize("role", ["jokr_scout", "jokr_app"])
def test_roles_cannot_create_in_public(db: Connection, role: str) -> None:
    can = db.execute(
        text("SELECT has_schema_privilege(:r, 'public', 'CREATE')"), {"r": role}
    ).scalar_one()
    assert can is False


def test_downgrade_refuses_while_signals_has_rows(
    database_url: str, migration_config: Config, engine: Engine
) -> None:
    with engine.begin() as conn:
        source_id = _source(conn, "downgrade_probe")
        conn.execute(INSERT_SIGNAL, _signal_params(source_id, _run(conn, source_id)))
    try:
        with pytest.raises(DBAPIError, match="refusing to downgrade"):
            command.downgrade(migration_config, "0001")
    finally:
        # The insert-only trigger blocks DELETE, so clean up the way an operator would.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE signals DISABLE TRIGGER signals_no_update_delete"))
            conn.execute(text("DELETE FROM signals WHERE source_id = :s"), {"s": source_id})
            conn.execute(text("ALTER TABLE signals ENABLE TRIGGER signals_no_update_delete"))
            conn.execute(text("DELETE FROM runs WHERE source_id = :s"), {"s": source_id})
            conn.execute(text("DELETE FROM sources WHERE id = :s"), {"s": source_id})
    assert "signals" in inspect(engine).get_table_names()


def test_downgrade_revokes_scout_grants(engine: Engine, migration_config: Config) -> None:
    command.downgrade(migration_config, "0001")
    try:
        with engine.connect() as conn:
            can = conn.execute(
                text("SELECT has_table_privilege('jokr_scout', 'decisions_log', 'INSERT')")
            ).scalar_one()
        assert can is False
    finally:
        command.upgrade(migration_config, "head")


@pytest.mark.parametrize(
    ("query", "index"),
    [
        (
            "SELECT id FROM signals ORDER BY intent_score DESC, points DESC NULLS LAST, "
            "num_comments DESC NULLS LAST, id LIMIT 20",
            "ix_signals_rank",
        ),
        (
            # The form Scout uses for its watermark.
            "SELECT posted_at FROM signals WHERE source_id = 1 "
            "ORDER BY posted_at DESC NULLS LAST LIMIT 1",
            "ix_signals_source_posted",
        ),
        (
            "SELECT content_hash, count(DISTINCT source_id) FROM signals "
            "GROUP BY content_hash ORDER BY content_hash",
            "ix_signals_content_hash",
        ),
    ],
    ids=["top-n", "watermark", "recurring-pain"],
)
def test_planned_queries_can_use_their_index(db: Connection, query: str, index: str) -> None:
    db.execute(text("SET LOCAL enable_seqscan = off"))
    db.execute(text("SET LOCAL enable_sort = off"))
    plan = "\n".join(db.execute(text(f"EXPLAIN {query}")).scalars())
    assert index in plan, plan
