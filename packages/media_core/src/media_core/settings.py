from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings shared by API and worker processes."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    media_storage_dir: Path = Path("./data")

