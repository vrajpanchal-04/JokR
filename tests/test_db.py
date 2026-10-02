"""Migration 0001: pgvector is on and decisions_log is append-only (C10)."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from jokr.db.models import DecisionLog


def test_vector_extension_is_installed(db: Connection) -> None:
    version = db.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).scalar_one()
    assert version


def test_vector_round_trip(db: Connection) -> None:
    db.execute(text("CREATE TEMP TABLE probe (v vector(3))"))
    db.execute(text("INSERT INTO probe VALUES ('[1,2,3]')"))
    distance = db.execute(text("SELECT v <-> '[1,2,4]' FROM probe")).scalar_one()
    assert distance == pytest.approx(1.0)


def _insert(db: Connection) -> int:
    with Session(bind=db, join_transaction_mode="create_savepoint") as session:
        row = DecisionLog(agent="chief", action="boot", reason="P0 smoke test", evidence_ids=[1, 2])
        session.add(row)
        session.commit()  # releases the savepoint; the outer test transaction still rolls back
        return row.id


def test_decisions_log_accepts_inserts(db: Connection) -> None:
    row_id = _insert(db)

    row = db.execute(select(DecisionLog).where(DecisionLog.id == row_id)).one()
    assert row.agent == "chief"
    assert row.evidence_ids == [1, 2]
    assert row.ts is not None


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE decisions_log SET reason = 'rewritten' WHERE id = :id",
        "DELETE FROM decisions_log WHERE id = :id",
        "TRUNCATE decisions_log",
    ],
    ids=["update", "delete", "truncate"],
)
def test_decisions_log_is_append_only(db: Connection, statement: str) -> None:
    row_id = _insert(db)

    savepoint = db.begin_nested()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text(statement), {"id": row_id})
    savepoint.rollback()

    count = db.execute(select(func.count()).select_from(DecisionLog)).scalar_one()
    assert count >= 1


def test_migrations_round_trip_from_zero(engine: Engine, migration_config: Config) -> None:
    """A8: downgrade to nothing and back up again must work cleanly."""
    command.downgrade(migration_config, "base")
    assert "decisions_log" not in inspect(engine).get_table_names()

    command.upgrade(migration_config, "head")
    assert "decisions_log" in inspect(engine).get_table_names()


# The API connects as jokr_app, not as the table owner. These tests prove that
# role can append to the log but cannot get around the trigger either.


def test_app_role_can_insert_and_read(db: Connection) -> None:
    db.execute(text("SET LOCAL ROLE jokr_app"))
    row_id = _insert(db)
    assert db.execute(select(DecisionLog.id).where(DecisionLog.id == row_id)).scalar_one()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE decisions_log SET reason = 'rewritten'",
        "DELETE FROM decisions_log",
        "ALTER TABLE decisions_log DISABLE TRIGGER decisions_log_no_update_delete",
        "DROP TABLE decisions_log",
        "SET session_replication_role = replica",
    ],
    ids=["update", "delete", "disable-trigger", "drop", "replication-role"],
)
def test_app_role_cannot_bypass_append_only(db: Connection, statement: str) -> None:
    db.execute(text("SET LOCAL ROLE jokr_app"))

    savepoint = db.begin_nested()
    with pytest.raises(DBAPIError, match=r"append-only|permission denied|must be owner"):
        db.execute(text(statement))
    savepoint.rollback()
