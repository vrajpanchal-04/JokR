"""Async engine factory (SQLAlchemy 2 + psycopg 3)."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(database_url: str) -> AsyncEngine:
    # A short connect timeout keeps /health fast when the DB is down.
    return create_async_engine(
        database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
    )
