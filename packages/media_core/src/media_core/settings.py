from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings shared by API and worker processes."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    media_storage_dir: Path = Path("./data")
    media_allowed_hosts: str = ""
    max_input_bytes: int = 512 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024 * 1024
    source_ttl_seconds: int = 3600
    job_retention_seconds: int = 86400
    queue_wait_seconds: int = 3600
    queue_capacity: int = 100
    job_timeout_seconds: int = 360
    max_job_attempts: int = 2
    retry_delay_seconds: int = 5

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(host.strip().lower() for host in self.media_allowed_hosts.split(",") if host.strip())

