"""Environment settings. Secrets come from the environment or .env, never from git."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jokr_config_dir: Path = Path("config")
