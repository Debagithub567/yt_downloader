from pathlib import Path

from media_core.settings import Settings


def test_settings_read_shared_environment(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://example.internal:6379/2")
    monkeypatch.setenv("MEDIA_STORAGE_DIR", "/tmp/media-test")

    settings = Settings()

    assert settings.redis_url == "redis://example.internal:6379/2"
    assert settings.media_storage_dir == Path("/tmp/media-test")

