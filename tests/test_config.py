"""C6 (budget caps) and friends: config must load strictly or not at all."""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from jokr.config import ConfigError, load_config

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the repo's real config files."""
    target = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, target)
    return target


def _edit(path: Path, change: dict[str, object]) -> None:
    data = yaml.safe_load(path.read_text())
    data.update(change)
    path.write_text(yaml.safe_dump(data))


def test_repo_config_loads_with_blueprint_defaults() -> None:
    cfg = load_config(REPO_CONFIG)

    assert cfg.caps.per_experiment_cad == Decimal("50")
    assert cfg.caps.per_month_cad == Decimal("200")
    assert cfg.caps.llm_per_day_usd == Decimal("5")
    assert cfg.caps.llm_alert_ratio == Decimal("0.8")
    assert cfg.caps.max_live_bets == 5
    assert len(cfg.fit_rules.rules) == 6
    assert cfg.scoring.g1_threshold == Decimal("6.5")
    assert all(not s.enabled for s in cfg.sources.sources)


def test_unknown_cap_key_is_rejected(config_dir: Path) -> None:
    _edit(config_dir / "caps.yaml", {"per_experiment_usd": 50})

    with pytest.raises(ConfigError, match=r"caps\.yaml"):
        load_config(config_dir)


@pytest.mark.parametrize(
    "key", ["per_experiment_cad", "per_month_cad", "llm_per_day_usd", "max_live_bets"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_cap_is_rejected(config_dir: Path, key: str, value: int) -> None:
    _edit(config_dir / "caps.yaml", {key: value})

    with pytest.raises(ConfigError, match=r"caps\.yaml"):
        load_config(config_dir)


@pytest.mark.parametrize("ratio", [0, 1.5, -0.1])
def test_alert_ratio_outside_unit_interval_is_rejected(config_dir: Path, ratio: float) -> None:
    _edit(config_dir / "caps.yaml", {"llm_alert_ratio": ratio})

    with pytest.raises(ConfigError, match=r"caps\.yaml"):
        load_config(config_dir)


def test_experiment_cap_above_month_cap_is_rejected(config_dir: Path) -> None:
    _edit(config_dir / "caps.yaml", {"per_experiment_cad": 500})

    with pytest.raises(ConfigError, match=r"caps\.yaml"):
        load_config(config_dir)


def test_scoring_weights_must_sum_to_one(config_dir: Path) -> None:
    path = config_dir / "scoring.yaml"
    data = yaml.safe_load(path.read_text())
    data["weights"]["pain_intensity"] = 0.30
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError, match=r"sum to 1\.0"):
        load_config(config_dir)


def test_scoring_rejects_unknown_factor(config_dir: Path) -> None:
    path = config_dir / "scoring.yaml"
    data = yaml.safe_load(path.read_text())
    data["weights"]["vibes"] = 0.0
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError, match=r"scoring\.yaml"):
        load_config(config_dir)


def test_duplicate_fit_rule_ids_are_rejected(config_dir: Path) -> None:
    path = config_dir / "fit_rules.yaml"
    data = yaml.safe_load(path.read_text())
    data["rules"][1]["id"] = data["rules"][0]["id"]
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError, match="duplicate"):
        load_config(config_dir)


def test_api_source_requires_tos_url(config_dir: Path) -> None:
    path = config_dir / "sources.yaml"
    data = yaml.safe_load(path.read_text())
    api_source = next(s for s in data["sources"] if s["type"] == "api")
    api_source.pop("tos_url")
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError, match="tos_url"):
        load_config(config_dir)


def test_missing_file_is_reported(config_dir: Path) -> None:
    (config_dir / "sources.yaml").unlink()

    with pytest.raises(ConfigError, match=r"sources\.yaml"):
        load_config(config_dir)


def test_config_error_keeps_validation_detail(config_dir: Path) -> None:
    _edit(config_dir / "caps.yaml", {"max_live_bets": -1})

    with pytest.raises(ConfigError) as info:
        load_config(config_dir)
    assert isinstance(info.value.__cause__, ValidationError)
