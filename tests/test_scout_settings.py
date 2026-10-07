"""Scout's secrets: required where they matter, never printable."""

import pytest
from pydantic import ValidationError

from jokr.settings import ScoutSettings, Settings

DB = "postgresql+psycopg://jokr_scout:x@db:5432/jokr"
SALT = "fixture-salt-0123456789abcdefghijKLMNOP"


def _scout(**overrides: object) -> ScoutSettings:
    values: dict[str, object] = {"database_url": DB, "author_hash_salt": SALT, **overrides}
    return ScoutSettings.model_validate(values)


def test_salt_is_required() -> None:
    with pytest.raises(ValidationError, match="author_hash_salt"):
        ScoutSettings.model_validate({"database_url": DB})


def test_short_salt_is_rejected() -> None:
    with pytest.raises(ValidationError, match="author_hash_salt"):
        _scout(author_hash_salt="too-short")


def test_secrets_are_not_printable() -> None:
    s = _scout(reddit_client_id="client-id-value", reddit_client_secret="client-secret-value")

    for text in (repr(s), str(s), str(s.model_dump())):
        assert SALT not in text
        assert "client-secret-value" not in text
        assert "client-id-value" not in text
    assert s.reddit_client_secret is not None
    assert s.reddit_client_secret.get_secret_value() == "client-secret-value"


def test_reddit_credentials_are_optional() -> None:
    s = _scout()
    assert s.reddit_client_id is None
    assert s.reddit_client_secret is None
    assert not s.reddit_configured


def test_reddit_configured_needs_both_parts_and_user_agent() -> None:
    assert not _scout(reddit_client_id="a").reddit_configured
    assert _scout(
        reddit_client_id="a",
        reddit_client_secret="b",
        reddit_user_agent="linux:jokr-scout:0.1 (by /u/someone)",
    ).reddit_configured


def test_reddit_user_agent_must_follow_reddit_format() -> None:
    with pytest.raises(ValidationError, match="reddit_user_agent"):
        _scout(reddit_user_agent="python-requests/2.0")


def test_api_settings_do_not_carry_scout_secrets() -> None:
    """The API process never sees Reddit keys or the author salt."""
    assert "author_hash_salt" not in Settings.model_fields
    assert not any(name.startswith("reddit") for name in Settings.model_fields)


def test_blank_reddit_values_mean_not_configured() -> None:
    s = _scout(reddit_client_id="", reddit_client_secret=" ", reddit_user_agent="")
    assert s.reddit_client_id is None
    assert not s.reddit_configured


def test_blank_proxy_settings_mean_no_proxy() -> None:
    s = _scout(scout_https_proxy="", scout_ca_bundle=" ")
    assert s.scout_https_proxy is None
    assert s.scout_ca_bundle is None


@pytest.mark.parametrize(
    "salt", ["replace-with-48-random-url-safe-characters-xxxxxxxxxx", "a" * 40, "ab" * 20]
)
def test_placeholder_or_guessable_salts_are_refused(salt: str) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _scout(author_hash_salt=salt)
