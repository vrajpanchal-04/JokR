"""Alembic migrations. Run with `alembic upgrade head` or `alembic_config(url)`."""

from pathlib import Path

from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent


def alembic_config(database_url: str) -> Config:
    """An Alembic config for this package that works from any working directory."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Alembic passes main options through ConfigParser, which treats % specially.
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg
