"""C3: sources.yaml is the single, strictly validated authority for what Scout may read."""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from jokr.config import ArxivParams, ConfigError, HackerNewsParams, Source, load_config
from tests.guard_helpers import api_source

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"

Edit = Callable[[dict[str, Any]], None]


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, target)
    return target


def _edit_source(config_dir: Path, name: str, change: Edit) -> None:
    path = config_dir / "sources.yaml"
    data = yaml.safe_load(path.read_text())
    change(next(s for s in data["sources"] if s["name"] == name))
    path.write_text(yaml.safe_dump(data))


def test_repo_sources_have_typed_params() -> None:
    sources = {s.name: s for s in load_config(REPO_CONFIG).sources.sources}

    assert set(sources) == {"hackernews", "reddit", "arxiv", "inbox"}
    assert isinstance(sources["hackernews"].params, HackerNewsParams)
    assert isinstance(sources["arxiv"].params, ArxivParams)
    assert sources["hackernews"].allowed_hosts == ("hn.algolia.com",)
    assert sources["arxiv"].allowed_hosts == ("export.arxiv.org",)
    assert set(sources["reddit"].allowed_hosts) == {"www.reddit.com", "oauth.reddit.com"}
    assert sources["inbox"].allowed_hosts == ()


def test_hn_tos_url_is_the_algolia_api_actually_called() -> None:
    hn = next(s for s in load_config(REPO_CONFIG).sources.sources if s.name == "hackernews")
    assert hn.tos_url is not None
    assert hn.tos_url.host == "hn.algolia.com"


def test_arxiv_spacing_respects_its_terms() -> None:
    """arXiv asks for no more than one request every three seconds."""
    arxiv = next(s for s in load_config(REPO_CONFIG).sources.sources if s.name == "arxiv")
    assert arxiv.min_interval_s is not None
    assert arxiv.min_interval_s >= 3


def test_get_source_returns_none_for_unknown(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.sources.get("hackernews") is not None
    assert cfg.sources.get("not_a_source") is None


@pytest.mark.parametrize("field", ["allowed_hosts", "tos_url", "min_interval_s", "max_requests"])
def test_api_source_requires_field(config_dir: Path, field: str) -> None:
    _edit_source(config_dir, "hackernews", lambda s: s.pop(field))

    with pytest.raises(ConfigError, match=field):
        load_config(config_dir)


def test_api_source_requires_at_least_one_host(config_dir: Path) -> None:
    _edit_source(config_dir, "hackernews", lambda s: s.update(allowed_hosts=[]))

    with pytest.raises(ConfigError, match="allowed_hosts"):
        load_config(config_dir)


@pytest.mark.parametrize(
    "host",
    [
        "https://hn.algolia.com",  # scheme
        "hn.algolia.com:443",  # port
        "HN.Algolia.com",  # not normalized
        "*.algolia.com",  # wildcard
        "10.0.0.1",  # IP literal
        "user@hn.algolia.com",  # userinfo
        "hn.algolia.com.",  # trailing dot
        "localhost",  # not a public FQDN
    ],
)
def test_allowed_host_must_be_a_plain_lowercase_fqdn(config_dir: Path, host: str) -> None:
    _edit_source(config_dir, "hackernews", lambda s: s.update(allowed_hosts=[host]))

    with pytest.raises(ConfigError, match="allowed_hosts"):
        load_config(config_dir)


def test_inbox_source_cannot_carry_hosts(config_dir: Path) -> None:
    _edit_source(config_dir, "inbox", lambda s: s.update(allowed_hosts=["example.com"]))

    with pytest.raises(ConfigError, match="inbox"):
        load_config(config_dir)


def test_params_kind_must_match_source(config_dir: Path) -> None:
    """A source can't borrow another connector's params (and so its behaviour)."""
    _edit_source(
        config_dir,
        "hackernews",
        lambda s: s.update(params={"kind": "arxiv", "categories": ["cs.AI"], "page_size": 10}),
    )

    with pytest.raises(ConfigError, match="params"):
        load_config(config_dir)


def test_unknown_param_is_rejected(config_dir: Path) -> None:
    _edit_source(config_dir, "arxiv", lambda s: s["params"].update(follow_links=True))

    with pytest.raises(ConfigError, match=r"sources\.yaml"):
        load_config(config_dir)


@pytest.mark.parametrize("value", [0, -1])
def test_max_requests_must_be_positive(config_dir: Path, value: int) -> None:
    _edit_source(config_dir, "arxiv", lambda s: s.update(max_requests=value))

    with pytest.raises(ConfigError, match="max_requests"):
        load_config(config_dir)


def test_min_interval_cannot_be_negative(config_dir: Path) -> None:
    _edit_source(config_dir, "arxiv", lambda s: s.update(min_interval_s=-1))

    with pytest.raises(ConfigError, match="min_interval_s"):
        load_config(config_dir)


def test_inbox_path_must_stay_inside_repo_data(config_dir: Path) -> None:
    _edit_source(config_dir, "inbox", lambda s: s.update(path="../../etc"))

    with pytest.raises(ConfigError, match="path"):
        load_config(config_dir)


def test_repo_sources_include_platform_review_additions() -> None:
    cfg = load_config(Path(__file__).resolve().parents[1] / "config")
    hn = cfg.sources.get("hackernews")
    reddit = cfg.sources.get("reddit")
    assert hn is not None and reddit is not None
    assert hn.params.kind == "hackernews"
    assert set(hn.params.browse_tags) == {"ask_hn", "show_hn"}
    assert reddit.params.kind == "reddit"
    assert {"startups", "SideProject", "Business_Ideas", "StartupIdeas"} <= set(
        reddit.params.subreddits
    )
    assert all(s.max_runtime_s <= 900 for s in cfg.sources.sources)


@pytest.mark.parametrize("value", [0, 3601])
def test_max_runtime_is_bounded(value: int) -> None:
    data = api_source().model_dump(mode="json")
    data["max_runtime_s"] = value
    with pytest.raises(ValidationError, match="max_runtime_s"):
        Source.model_validate(data)


def test_max_runtime_defaults_to_five_minutes() -> None:
    assert api_source().max_runtime_s == 300
