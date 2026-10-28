"""Fast API and queue admission checks without Redis, RQ, or network services."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from media_api import routes
from media_core.errors import ErrorCode, MediaError
from media_core.jobs import sweep_expired_storage
from media_core.models import AudioStream, MediaInfo
from media_core.settings import Settings
from media_core.sources import StagedMedia
from media_worker import main as worker_main
from rq import Worker


class MemoryRedis:
    def __init__(self):
        self.records = {}
        self.unavailable = False
        self.locks = set()

    def get(self, key):
        if self.unavailable:
            raise ConnectionError("private Redis connection detail")
        return self.records.get(key)

    def set(self, key, value, *, ex=None, nx=False):
        if self.unavailable:
            raise ConnectionError("private Redis connection detail")
        if nx and key in self.records:
            return False
        self.records[key] = value
        return True

    def delete(self, key):
        self.records.pop(key, None)

    def ttl(self, key):
        return 3600 if key in self.records else -2

    def lock(self, name, **_kwargs):
        redis = self

        class Lock:
            def acquire(self, blocking=True):
                if name in redis.locks:
                    return False
                redis.locks.add(name)
                return True

            def release(self):
                redis.locks.remove(name)

            def __enter__(self):
                if not self.acquire():
                    raise RuntimeError("Lock unavailable")
                return self

            def __exit__(self, *_args):
                self.release()

        return Lock()

    def pipeline(self):
        redis = self

        class Pipeline:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def watch(self, _key):
                pass

            def get(self, key):
                return redis.get(key)

            def multi(self):
                pass

            def set(self, key, value, *, ex=None):
                redis.set(key, value, ex=ex)

            def execute(self):
                pass

        return Pipeline()


class MemoryQueue:
    def __init__(self):
        self.scheduled = []
        self.enqueued = []
        self.length = 0
        self.fail_schedule = None
        self.empty_schedule = None
        self.fail_enqueue = False

    def __len__(self):
        return self.length

    def enqueue_in(self, delay, function, identifier):
        if function == self.fail_schedule:
            raise ConnectionError("private scheduler detail")
        if function == self.empty_schedule:
            return None
        self.scheduled.append((delay, function, identifier))
        return SimpleNamespace(id="cleanup")

    def enqueue(self, function, identifier, **kwargs):
        if self.fail_enqueue:
            raise ConnectionError("private enqueue detail")
        self.enqueued.append((function, identifier, kwargs))
        return SimpleNamespace(id=identifier)


@pytest.fixture
def api(tmp_path: Path, monkeypatch):
    redis = MemoryRedis()
    queue = MemoryQueue()
    media = MediaInfo(title="test", duration_seconds=1, file_size=5, container="m4a",
                      audio_streams=(AudioStream(index=0, codec="aac"),))
    settings = Settings(media_storage_dir=tmp_path, media_allowed_hosts="media.example",
                        max_input_bytes=16, queue_capacity=1)
    monkeypatch.setattr(routes, "Queue", lambda *_args, **_kwargs: queue)
    monkeypatch.setattr(routes, "inspect_file", lambda _path: media)
    client = TestClient(routes.create_app(redis_client=redis, settings=settings))
    return SimpleNamespace(client=client, redis=redis, queue=queue, root=tmp_path, media=media)


def upload(api, *, data=b"audio", filename="tone.m4a"):
    return api.client.post("/api/v1/media/inspect", content=data,
                           headers={"content-type": "application/octet-stream", "x-filename": filename})


def submit(api, source, **overrides):
    body = {"source_id": source["source_id"], "source_token": source["source_token"],
            "output_type": "audio", "output_format": "m4a"}
    body.update(overrides)
    return api.client.post("/api/v1/jobs", json=body)


def test_authorized_url_inspection(api, monkeypatch):
    def stage(source, url, destination):
        assert source.allowed_hosts == frozenset({"media.example"})
        assert url == "https://media.example/tone.m4a"
        path = destination / "tone.m4a"
        path.write_bytes(b"audio")
        return StagedMedia(path, path.name)

    monkeypatch.setattr(routes.DirectURLSource, "stage", stage)
    response = api.client.post("/api/v1/media/inspect", json={"url": "https://media.example/tone.m4a"})
    assert response.status_code == 201
    assert response.json()["media"]["audio_streams"][0]["codec"] == "aac"
    assert response.json()["audio_options"] == ["m4a", "mp3", "original"]
    assert api.queue.scheduled[0][1] == "media_worker.jobs.cleanup_source"


def test_streamed_upload_and_safe_filename(api):
    response = upload(api, filename="../../unsafe.m4a")
    assert response.status_code == 201
    source = response.json()
    directory = api.root / "sources" / source["source_id"]
    assert (directory / "unsafe.m4a").read_bytes() == b"audio"
    assert (directory / ".expires").is_file()
    assert "path" not in str(source)


@pytest.mark.parametrize("url", ["http://media.example/a.mp4", "https://other.example/a.mp4"])
def test_invalid_url_is_rejected_without_network(api, url):
    response = api.client.post("/api/v1/media/inspect", json={"url": url})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "unauthorized_source"
    assert not list((api.root / "sources").iterdir())


def test_invalid_media_is_rejected_and_removed(api, monkeypatch):
    def invalid(_path):
        raise MediaError(ErrorCode.INVALID_MEDIA, "Source is not a valid media file")

    monkeypatch.setattr(routes, "inspect_file", invalid)
    response = upload(api)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_media"
    assert not list((api.root / "sources").iterdir())


def test_upload_size_limit(api):
    response = upload(api, data=b"x" * 17)
    assert response.status_code == 413
    assert not list((api.root / "sources").iterdir())


def test_url_request_size_limit(api):
    response = api.client.post("/api/v1/media/inspect",
                               content=b"x" * (16 * 1024 + 1),
                               headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert not list((api.root / "sources").iterdir())


def test_valid_job_submission_and_authorized_status(api):
    source = upload(api).json()
    response = submit(api, source)
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "queued"
    assert api.queue.enqueued[0][0] == "media_worker.jobs.process_media_job"
    assert api.queue.scheduled[-1][1] == "media_worker.jobs.cleanup_job"
    assert (api.root / "jobs" / accepted["job_id"] / ".expires").is_file()
    status = api.client.get(accepted["status_url"], headers={"authorization": f"Bearer {accepted['access_token']}"})
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert "token" not in str(status.json())
    assert "source_filename" not in status.json()
    assert "output" not in status.json()


def test_invalid_processing_options(api):
    source = upload(api).json()
    response = submit(api, source, output_format="mp4")
    assert response.status_code == 400
    assert not api.queue.enqueued


def test_invalid_job_request_schema(api):
    response = api.client.post("/api/v1/jobs", json={"source_id": "../secret", "source_token": "x",
                                                      "output_type": "audio", "output_format": "m4a"})
    assert response.status_code == 422


def test_queue_full_cleans_staging(api):
    source = upload(api).json()
    api.queue.length = 1
    response = submit(api, source)
    assert response.status_code == 429
    assert not api.queue.enqueued
    assert not list((api.root / "jobs").iterdir())


def test_job_id_collision_does_not_remove_existing_directory(api, monkeypatch, tmp_path):
    source = upload(api).json()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    job_id = "e" * 32
    parent = api.root / "jobs"
    parent.mkdir(exist_ok=True)
    (parent / job_id).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(routes, "new_id", lambda: job_id)
    assert submit(api, source).status_code == 503
    assert (outside / "keep").read_text() == "safe"
    assert (parent / job_id).is_symlink()


def test_redis_unavailable_has_safe_response(api):
    source = upload(api).json()
    api.redis.unavailable = True
    response = submit(api, source)
    assert response.status_code == 503
    assert "private" not in response.text


def test_enqueue_failure_cancels_record_and_keeps_recoverable_cleanup(api, monkeypatch):
    source = upload(api).json()
    api.queue.fail_enqueue = True
    monkeypatch.setattr(routes.Job, "fetch", lambda *_args, **_kwargs: MagicMock())
    response = submit(api, source)
    assert response.status_code == 503
    assert "private" not in response.text
    keys = [key for key in api.redis.records if key.startswith("media:job:")]
    assert len(keys) == 1
    record = json.loads(api.redis.records[keys[0]])
    assert record["status"] == "cancelled"
    assert api.queue.scheduled[-1][1] == "media_worker.jobs.cleanup_job"
    assert (api.root / "jobs" / record["id"] / ".expires").is_file()
    marker = api.root / "jobs" / record["id"] / ".expires"
    assert sweep_expired_storage(api.root, api.redis, now=float(marker.read_text()) + 1) == 1


def test_source_cleanup_schedule_failure_rolls_back(api):
    api.queue.fail_schedule = "media_worker.jobs.cleanup_source"
    response = upload(api)
    assert response.status_code == 503
    assert "private" not in response.text
    assert not api.redis.records
    assert not list((api.root / "sources").iterdir())


def test_job_cleanup_schedule_failure_rolls_back_before_enqueue(api):
    source = upload(api).json()
    api.queue.fail_schedule = "media_worker.jobs.cleanup_job"
    response = submit(api, source)
    assert response.status_code == 503
    assert not api.queue.enqueued
    assert not list((api.root / "jobs").iterdir())
    assert not [key for key in api.redis.records if key.startswith("media:job:")]


def test_missing_cleanup_task_is_treated_as_failure(api):
    source = upload(api).json()
    api.queue.empty_schedule = "media_worker.jobs.cleanup_job"
    response = submit(api, source)
    assert response.status_code == 503
    assert not api.queue.enqueued
    assert not list((api.root / "jobs").iterdir())


def test_status_auth_unknown_and_expired(api):
    source = upload(api).json()
    accepted = submit(api, source).json()
    url = accepted["status_url"]
    token = {"authorization": f"Bearer {accepted['access_token']}"}
    assert api.client.get(url).status_code == 401
    assert api.client.get(url, headers={"authorization": "Bearer wrong"}).status_code == 404
    assert api.client.get("/api/v1/jobs/" + "f" * 32, headers=token).status_code == 404
    key = f"media:job:{accepted['job_id']}"
    record = json.loads(api.redis.records[key])
    record["expires_epoch"] = 0
    api.redis.records[key] = json.dumps(record)
    assert api.client.get(url, headers=token).status_code == 410


def test_status_redis_unavailable_does_not_leak_connection_details(api):
    source = upload(api).json()
    accepted = submit(api, source).json()
    api.redis.unavailable = True
    response = api.client.get(accepted["status_url"],
                              headers={"authorization": f"Bearer {accepted['access_token']}"})
    assert response.status_code == 503
    assert "private" not in response.text


def test_worker_maintenance_recovers_expired_files(api, monkeypatch):
    source = upload(api).json()
    accepted = submit(api, source).json()
    directory = api.root / "jobs" / accepted["job_id"]
    marker = float((directory / ".expires").read_text())
    monkeypatch.setattr(Worker, "run_maintenance_tasks", lambda _self: None)
    monkeypatch.setattr(worker_main, "Settings", lambda: Settings(media_storage_dir=api.root))
    monkeypatch.setattr(worker_main, "sweep_expired_storage",
                        lambda root, redis, **_kwargs: sweep_expired_storage(root, redis, now=marker + 1))
    worker = object.__new__(worker_main.CleanupWorker)
    worker.connection = api.redis
    worker.run_maintenance_tasks()
    assert not directory.exists()
