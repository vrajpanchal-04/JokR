"""GET /health is a real check: DB round trip, pgvector, config."""

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from jokr.api.main import create_app
from jokr.config import ConfigError
from jokr.settings import Settings

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"
# Nothing listens on port 1, so the connection is refused immediately.
DEAD_DB_URL = "postgresql+psycopg://jokr:jokr@127.0.0.1:1/jokr"


def _settings(database_url: str, config_dir: Path = REPO_CONFIG) -> Settings:
    return Settings(database_url=database_url, jokr_config_dir=config_dir)


def test_health_ok(database_url: str, engine: Engine) -> None:
    with TestClient(create_app(_settings(database_url))) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["config"] == "ok"
    assert body["pgvector"].count(".") >= 1


def test_health_503_when_db_unreachable() -> None:
    with TestClient(create_app(_settings(DEAD_DB_URL))) as client:
        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["db"] == "error"
    assert body["config"] == "ok"


def test_health_503_when_config_breaks_at_runtime(
    tmp_path: Path, database_url: str, engine: Engine
) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, config_dir)

    with TestClient(create_app(_settings(database_url, config_dir))) as client:
        (config_dir / "caps.yaml").write_text(yaml.safe_dump({"per_month_cad": -1}))
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["config"] == "error"


def test_app_refuses_to_start_on_bad_caps(tmp_path: Path) -> None:
    """C6: a bad caps.yaml stops startup instead of running uncapped."""
    config_dir = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, config_dir)
    (config_dir / "caps.yaml").write_text(yaml.safe_dump({"per_month_cad": -1}))

    with pytest.raises(ConfigError), TestClient(create_app(_settings(DEAD_DB_URL, config_dir))):
        pass
