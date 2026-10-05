"""Compose wiring for Scout: secrets only where needed, inbox read-only."""

from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _services() -> dict[str, Any]:
    services: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())["services"]
    return services


def test_api_never_sees_scout_secrets() -> None:
    env = _services()["api"]["environment"]
    assert not any(k.startswith(("REDDIT", "AUTHOR_HASH", "SCOUT")) for k in env)


def test_scout_runs_as_its_own_role_with_a_read_only_inbox() -> None:
    scout = _services()["scout"]
    assert scout["environment"]["DATABASE_URL"] == "${SCOUT_DATABASE_URL:?set in .env}"
    assert "./data/inbox:/data/inbox:ro" in scout["volumes"]
    assert scout["profiles"] == ["jobs"]
    assert scout["healthcheck"] == {"disable": True}
    assert "MIGRATION_DATABASE_URL" not in scout["environment"]


def test_migrate_syncs_sources_after_migrating() -> None:
    command = " ".join(_services()["migrate"]["command"])
    assert command.index("alembic upgrade head") < command.index("jokr sources sync")
