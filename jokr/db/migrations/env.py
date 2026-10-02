"""Alembic environment.

URL order: sqlalchemy.url if the caller set one, then MIGRATION_DATABASE_URL
(the table owner), then DATABASE_URL. In compose the API's DATABASE_URL is the
restricted jokr_app role, which cannot run DDL.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from jokr.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

url = (
    config.get_main_option("sqlalchemy.url")
    or os.environ.get("MIGRATION_DATABASE_URL")
    or os.environ["DATABASE_URL"]
)


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
