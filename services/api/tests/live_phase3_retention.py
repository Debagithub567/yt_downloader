"""Opt-in short-retention checks in an isolated Compose API and worker pair.

The launcher must set Redis DB 15, a unique /data/milestone-e-* root, and
JOB_RETENTION_SECONDS=3. This script refuses the ordinary application config.
"""

import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.registry import StartedJobRegistry

from media_core.jobs import (JobStore, cleanup_expired_job, mark_expiration, new_id, new_token,
                             remove_storage_directory, safe_directory, sweep_expired_storage)
from media_core.settings import Settings


SETTINGS = Settings()
assert SETTINGS.redis_url.endswith("/15")
assert SETTINGS.media_storage_dir.parent == Path("/data")
assert SETTINGS.media_storage_dir.name.startswith("milestone-e-")
assert SETTINGS.job_retention_seconds == 3
ROOT = SETTINGS.media_storage_dir
REDIS = Redis.from_url(SETTINGS.redis_url)
STORE = JobStore(REDIS, SETTINGS)
API = "http://127.0.0.1:8000"


def http(method: str, path: str, body: bytes | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    try:
        with urlopen(Request(API + path, data=body, headers=headers or {}, method=method), timeout=15) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def await_condition(check, *, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for isolated retention condition")


def check_scheduled_cleanup() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "short-tone.m4a"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "1", "-c:a", "aac", str(source),
        ], check=True, timeout=20)
        code, body = http("POST", "/api/v1/media/inspect", source.read_bytes(), {
            "Content-Type": "application/octet-stream", "X-Filename": source.name,
        })
        assert code == 201, (code, body)
        inspected = json.loads(body)
        request = {"source_id": inspected["source_id"], "source_token": inspected["source_token"],
                   "output_type": "audio", "output_format": "m4a"}
        code, body = http("POST", "/api/v1/jobs", json.dumps(request).encode(),
                          {"Content-Type": "application/json"})
        assert code == 202, (code, body)
        accepted = json.loads(body)
        job_id = accepted["job_id"]
        token = accepted["access_token"]
        path = safe_directory(ROOT, "jobs", job_id)
        auth = {"Authorization": "Bearer " + token}

        def completed() -> bool:
            status, body = http("GET", f"/api/v1/jobs/{job_id}", headers=auth)
            return status == 200 and json.loads(body)["status"] == "completed"

        await_condition(completed)
        record = STORE.get(job_id)
        assert record and record["result"] and path.is_dir()
        assert Job.fetch(job_id, connection=REDIS).origin == "media"
        result = record["result"]
        output = path / "output" / result["filename"]
        assert output.is_file() and hashlib.sha256(output.read_bytes()).hexdigest() == result["sha256"]
        code, _ = http("GET", f"/api/v1/jobs/{job_id}/download", headers=auth)
        assert code == 200

        def expired_and_cleaned() -> bool:
            status, _ = http("GET", f"/api/v1/jobs/{job_id}/download", headers=auth)
            return status in (404, 410) and not path.exists() and STORE.get(job_id) is None

        await_condition(expired_and_cleaned, seconds=25)
        duplicate = Queue("media", connection=REDIS).enqueue(
            "media_worker.jobs.cleanup_job", job_id, job_id="duplicate-" + new_id(),
        )
        await_condition(lambda: Job.fetch(duplicate.id, connection=REDIS).get_status() == "finished")
        assert not path.exists() and STORE.get(job_id) is None
        print("scheduled cleanup and duplicate cleanup:", job_id, "passed")


def expired_directory(identifier: str) -> Path:
    path = safe_directory(ROOT, "jobs", identifier)
    path.mkdir(parents=True)
    (path / "partial.bin").write_bytes(b"test-only")
    mark_expiration(path, time.time() - 1)
    return path


def check_locks_and_recovery() -> None:
    job_id = new_id()
    path = expired_directory(job_id)
    lock = REDIS.lock(f"media:job:worker:{job_id}", timeout=30, blocking=False)
    assert lock.acquire(blocking=False)
    try:
        deleted, delay = cleanup_expired_job(ROOT, REDIS, SETTINGS, job_id)
        assert not deleted and delay and path.exists()
    finally:
        lock.release()
    deleted, delay = cleanup_expired_job(ROOT, REDIS, SETTINGS, job_id)
    assert deleted and delay is None and not path.exists()

    job_id = new_id()
    path = expired_directory(job_id)
    token = new_token()
    record = STORE.create_job(job_id, token, new_id(), "source.m4a",
                              {"output_type": "audio", "output_format": "m4a"})
    record.update(status="processing", claim_id=new_id(), claim_deadline=time.time() + 30,
                  expires_epoch=time.time() - 1)
    REDIS.set(f"media:job:{job_id}", json.dumps(record), ex=30)
    deleted, delay = cleanup_expired_job(ROOT, REDIS, SETTINGS, job_id)
    assert not deleted and delay and path.exists()
    record.update(status="cancelled", claim_deadline=None)
    REDIS.set(f"media:job:{job_id}", json.dumps(record), ex=30)
    deleted, delay = cleanup_expired_job(ROOT, REDIS, SETTINGS, job_id)
    assert deleted and delay is None and not path.exists() and STORE.get(job_id) is None

    job_id = new_id()
    path = expired_directory(job_id)
    assert sweep_expired_storage(ROOT, REDIS, settings=SETTINGS) >= 1
    assert not path.exists()

    job_id = new_id()
    path = expired_directory(job_id)
    (path / ".expires").unlink()
    marker = path.parent / f".deleting-{job_id}"
    marker.write_text(job_id, encoding="ascii")
    assert sweep_expired_storage(ROOT, REDIS, settings=SETTINGS) >= 1
    assert not path.exists() and not marker.exists()

    job_id = new_id()
    path = safe_directory(ROOT, "jobs", job_id)
    path.mkdir(parents=True)
    assert sweep_expired_storage(ROOT, REDIS, settings=SETTINGS) == 0 and path.exists()
    remove_storage_directory(path, time.time())
    print("locks, processing claim, missed expiry, tombstone, markerless preservation: passed")


def check_redis_delete_retry() -> None:
    job_id = new_id()
    path = expired_directory(job_id)
    record = STORE.create_job(job_id, new_token(), new_id(), "source.m4a",
                              {"output_type": "audio", "output_format": "m4a"})
    record["expires_epoch"] = time.time() - 1
    REDIS.set(f"media:job:{job_id}", json.dumps(record), ex=30)
    original_delete = REDIS.delete

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("injected test-only Redis delete failure")

    REDIS.delete = unavailable
    try:
        try:
            cleanup_expired_job(ROOT, REDIS, SETTINGS, job_id)
        except ConnectionError:
            pass
        else:
            raise AssertionError("Injected Redis delete failure did not occur")
    finally:
        REDIS.delete = original_delete
    assert not path.exists() and STORE.get(job_id) is not None
    deleted, delay = cleanup_expired_job(ROOT, REDIS, SETTINGS, job_id)
    assert deleted and delay is None and STORE.get(job_id) is None
    print("Redis delete failure retry:", job_id, "passed")


if __name__ == "__main__":
    assert REDIS.ping()
    assert len(Queue("media", connection=REDIS)) == 0
    assert len(StartedJobRegistry("media", connection=REDIS)) == 0
    assert not any(REDIS.scan_iter("media:*")), "Isolated Redis database contains media records"
    ROOT.mkdir(parents=True, exist_ok=True)
    for category in ("jobs", "sources"):
        directory = ROOT / category
        assert not directory.exists() or not any(directory.iterdir()), "Isolated test root is not empty"
    check_scheduled_cleanup()
    check_locks_and_recovery()
    check_redis_delete_retry()
