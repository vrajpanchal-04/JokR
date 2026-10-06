"""Environment settings. Secrets come from the environment or .env, never from git."""

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Reddit requires "<platform>:<app id>:<version> (by /u/<username>)".
_REDDIT_UA = r"^[a-z0-9_-]+:[A-Za-z0-9._-]+:[A-Za-z0-9._-]+ \(by /u/[A-Za-z0-9_-]{3,20}\)$"


class Settings(BaseSettings):
    """Settings every process needs. The API uses only these."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jokr_config_dir: Path = Path("config")


class ScoutSettings(Settings):
    """Scout-only settings. Kept separate so the API never loads these secrets."""

    # Keys the HMAC that hides author names (P1 plan §3). No default: a missing
    # or weak salt must stop Scout rather than store guessable hashes.
    author_hash_salt: SecretStr
    reddit_client_id: SecretStr | None = None
    reddit_client_secret: SecretStr | None = None
    reddit_user_agent: Annotated[str, Field(pattern=_REDDIT_UA)] | None = None
    inbox_root: Path = Path("data/inbox")
    # Opt-in egress proxy and CA bundle, for networks that only allow a proxy.
    # GuardedClient ignores the environment, so these are the only way to set one.
    scout_https_proxy: str | None = None
    scout_ca_bundle: Path | None = None

    @field_validator(
        "reddit_client_id",
        "reddit_client_secret",
        "reddit_user_agent",
        "scout_https_proxy",
        "scout_ca_bundle",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `.env.example` ships these as `KEY=`; an empty value means "not configured".
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("author_hash_salt")
    @classmethod
    def _salt_is_strong(cls, value: SecretStr) -> SecretStr:
        salt = value.get_secret_value()
        if len(salt.encode()) < 32:
            raise ValueError("author_hash_salt must be at least 32 bytes")
        # The .env.example placeholder, or anything as guessable, keys nothing.
        if "replace-with" in salt or len(set(salt)) < 16:
            raise ValueError("author_hash_salt looks like a placeholder; generate a random one")
        return value

    @property
    def reddit_configured(self) -> bool:
        return None not in (
            self.reddit_client_id,
            self.reddit_client_secret,
            self.reddit_user_agent,
        )
