"""Shared fixtures.

DB tests need a Postgres 16 + pgvector server. Point TEST_DATABASE_URL at one
(make test does this inside compose). The fixture creates a throwaway database
on that server, migrates it from zero, and drops it afterwards, so a dev
database is never touched.
"""

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from jokr.db.migrations import alembic_config

DEFAULT_TEST_URL = "postgresql+psycopg://jokr:jokr@127.0.0.1:5432/postgres"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    server_url = make_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL))
    name = f"jokr_test_{uuid4().hex[:8]}"
    admin = create_engine(server_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = server_url.set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def migration_config(database_url: str) -> Config:
    return alembic_config(database_url)


@pytest.fixture(scope="session")
def engine(database_url: str, migration_config: Config) -> Iterator[Engine]:
    command.upgrade(migration_config, "head")
    eng = create_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is rolled back after each test."""
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            yield conn
        finally:
            tx.rollback()
