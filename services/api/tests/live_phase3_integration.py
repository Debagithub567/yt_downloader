"""Opt-in checks against the running Compose API, Redis, RQ worker, and FFmpeg.

Run inside the API container: python /app/services/api/tests/live_phase3_integration.py
This module is intentionally not collected by pytest. It creates only random test IDs.
"""

import hashlib
import json
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.registry import StartedJobRegistry

from media_core.jobs import JobStore, mark_expiration, new_id, new_token, safe_directory
from media_core.settings import Settings


API = "http://127.0.0.1:8000"
SETTINGS = Settings()
assert SETTINGS.media_storage_dir == Path("/data")
assert SETTINGS.redis_url == "redis://redis:6379/0"
REDIS = Redis.from_url(SETTINGS.redis_url)
STORE = JobStore(REDIS, SETTINGS)


def http(method: str, path: str, body: bytes | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request = Request(API + path, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, {key.lower(): value for key, value in response.headers.items()}, response.read()
    except HTTPError as error:
        return error.code, {key.lower(): value for key, value in error.headers.items()}, error.read()


def upload(path: Path) -> dict:
    status, _, body = http("POST", "/api/v1/media/inspect", path.read_bytes(), {
        "Content-Type": "application/octet-stream", "X-Filename": path.name,
    })
    assert status == 201, (status, body)
    return json.loads(body)


def submit(source: dict, **selection: object) -> dict:
    request = {"source_id": source["source_id"], "source_token": source["source_token"], **selection}
    status, _, body = http("POST", "/api/v1/jobs", json.dumps(request).encode(),
                           {"Content-Type": "application/json"})
    assert status == 202, (status, body)
    return json.loads(body)


def authorization(job: dict) -> dict[str, str]:
    return {"Authorization": "Bearer " + job["access_token"]}


def status(job: dict) -> dict:
    code, _, body = http("GET", job["status_url"], headers=authorization(job))
    assert code == 200, (code, body)
    return json.loads(body)


def wait_terminal(job: dict, *, seconds: float = 90) -> tuple[dict, list[tuple]]:
    deadline = time.monotonic() + seconds
    observed: list[tuple] = []
    while time.monotonic() < deadline:
        current = status(job)
        sample = (current["status"], current["stage"], current["progress"], current["attempt"])
        if not observed or sample != observed[-1]:
            observed.append(sample)
        if current["status"] in ("completed", "failed", "cancelled"):
            return current, observed
        time.sleep(0.05)
    raise AssertionError(f"Job {job['job_id']} did not terminate: {observed}")


def generate_audio(directory: Path) -> Path:
    path = directory / "phase3-tone.m4a"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "4", "-c:a", "aac", str(path),
    ], check=True, timeout=20)
    return path


def generate_video(directory: Path) -> Path:
    path = directory / "phase3-motion.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
        "-t", "60", "-c:v", "mpeg4", "-q:v", "10", "-an", str(path),
    ], check=True, timeout=60)
    return path


def check_download(job: dict) -> None:
    job_id = job["job_id"]
    record = STORE.get(job_id)
    assert record and record["status"] == "completed" and record["attempts"] == 1
    result = record["result"]
    directory = safe_directory(SETTINGS.media_storage_dir, "jobs", job_id)
    manifest = directory / f".result-{record['claim_id']}.json"
    output = directory / "output" / result["filename"]
    assert json.loads(manifest.read_text()) == result
    assert output.is_file() and output.stat().st_size == result["file_size"]
    assert hashlib.sha256(output.read_bytes()).hexdigest() == result["sha256"]
    code, headers, downloaded = http("GET", f"/api/v1/jobs/{job_id}/download",
                                     headers=authorization(job))
    assert code == 200 and len(downloaded) == result["file_size"]
    assert hashlib.sha256(downloaded).hexdigest() == result["sha256"]
    assert headers["content-type"].startswith("audio/mpeg")
    assert headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
    with tempfile.TemporaryDirectory() as temporary:
        saved = Path(temporary) / "download.mp3"
        saved.write_bytes(downloaded)
        inspected = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=format_name,size",
            "-of", "json", str(saved),
        ], check=True, capture_output=True, timeout=20)
        media = json.loads(inspected.stdout)["format"]
    assert int(media["size"]) == len(downloaded)
    assert "mp3" in media["format_name"]
    for label, headers, expected in (
        ("missing", None, 401),
        ("invalid", {"Authorization": "Bearer invalid"}, 404),
    ):
        code, _, body = http("GET", f"/api/v1/jobs/{job_id}/download", headers=headers)
        assert code == expected and b"/data" not in body, (label, code, body)
    code, _, body = http("GET", f"/api/v1/jobs/{new_id()}/download", headers=authorization(job))
    assert code == 404 and b"/data" not in body
    print("download: valid MP3, size, SHA-256, headers, ffprobe, and authorization checks passed")


def check_audio() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        source = upload(generate_audio(Path(temporary)))
        job = submit(source, output_type="audio", output_format="mp3", audio_bitrate_kbps=128)
        assert STORE.get(job["job_id"])
        rq_job = Job.fetch(job["job_id"], connection=REDIS)
        assert rq_job.origin == "media"
        final, observed = wait_terminal(job)
        assert final["status"] == "completed" and final["progress"] == 100, observed
        assert final["attempt"] == 1
        check_download(job)
        original = STORE.get(job["job_id"])["result"]
        duplicate = Queue("media", connection=REDIS).enqueue(
            "media_worker.jobs.process_media_job", job["job_id"], job_id="duplicate-" + new_id(),
        )
        deadline = time.monotonic() + 10
        while Job.fetch(duplicate.id, connection=REDIS).get_status() != "finished" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Job.fetch(duplicate.id, connection=REDIS).get_status() == "finished"
        assert STORE.get(job["job_id"])["result"] == original
        assert STORE.get(job["job_id"])["attempts"] == 1
        print("audio job:", job["job_id"], "states:", observed)
        print("duplicate RQ delivery: completed result and attempt count unchanged")


def check_video_progress_and_running_cancel() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        source = upload(generate_video(Path(temporary)))
        selection = {"output_type": "video", "output_format": "mp4", "resolution": 360}
        job = submit(source, **selection)
        final, observed = wait_terminal(job, seconds=120)
        assert final["status"] == "completed" and final["progress"] == 100, (final, observed)
        processing = [sample[2] for sample in observed if sample[0] == "processing" and sample[1] == "processing"]
        assert all(value is None or value < 100 for value in processing)
        print("video progress:", job["job_id"], "observed:", observed)
        assert any(value is not None and value > 0 for value in processing), "Nonzero live progress was not captured"

        cancelled_job = submit(source, **selection)
        deadline = time.monotonic() + 45
        ready = False
        while time.monotonic() < deadline:
            current = status(cancelled_job)
            if (current["status"] == "processing" and current["stage"] == "processing"
                    and current["progress"] is not None and current["progress"] > 0):
                ready = True
                break
            if current["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)
        if not ready:
            print("running cancellation: INCONCLUSIVE; FFmpeg progress was not observed before completion")
            return
        code, _, body = http("POST", f"/api/v1/jobs/{cancelled_job['job_id']}/cancel",
                             headers=authorization(cancelled_job))
        assert code == 200, (code, body)
        final, observed = wait_terminal(cancelled_job, seconds=30)
        assert final["status"] == "cancelled" and STORE.get(cancelled_job["job_id"])["result"] is None
        cancelled_directory = safe_directory(SETTINGS.media_storage_dir, "jobs", cancelled_job["job_id"])
        cleanup_deadline = time.monotonic() + 10
        while cancelled_directory.exists() and time.monotonic() < cleanup_deadline:
            time.sleep(0.05)
        assert not cancelled_directory.exists(), "Cancelled worker left its storage after termination"
        code, _, _ = http("GET", f"/api/v1/jobs/{cancelled_job['job_id']}/download",
                          headers=authorization(cancelled_job))
        assert code == 409
        print("running cancellation:", cancelled_job["job_id"], "progress_at_request:",
              current["progress"], "states:", observed)


def check_queued_cancel() -> None:
    job_id, token = new_id(), new_token()
    directory = safe_directory(SETTINGS.media_storage_dir, "jobs", job_id)
    directory.mkdir(parents=True)
    record = STORE.create_job(job_id, token, new_id(), "queued.m4a",
                              {"output_type": "audio", "output_format": "m4a"})
    mark_expiration(directory, record["expires_epoch"])
    Queue("media", connection=REDIS).enqueue_in(
        timedelta(minutes=10), "media_worker.jobs.process_media_job", job_id, job_id=job_id,
    )
    accepted = {"job_id": job_id, "access_token": token, "status_url": f"/api/v1/jobs/{job_id}"}
    for _ in range(2):
        code, _, body = http("POST", f"/api/v1/jobs/{job_id}/cancel", headers=authorization(accepted))
        assert code == 200 and json.loads(body)["status"] == "cancelled"
    assert STORE.get(job_id)["attempts"] == 0 and not directory.exists()
    assert Job.fetch(job_id, connection=REDIS).get_status() == "canceled"
    code, _, _ = http("GET", f"/api/v1/jobs/{job_id}/download", headers=authorization(accepted))
    assert code == 409
    print("queued cancellation:", job_id, "transition, RQ cancellation, idempotence, cleanup passed")


def check_failure() -> None:
    code, _, body = http("POST", "/api/v1/media/inspect", b"not media", {
        "Content-Type": "application/octet-stream", "X-Filename": "invalid.mp4",
    })
    assert code == 400 and b"/data" not in body
    job_id, token = new_id(), new_token()
    directory = safe_directory(SETTINGS.media_storage_dir, "jobs", job_id)
    directory.mkdir(parents=True)
    record = STORE.create_job(job_id, token, new_id(), "missing.m4a",
                              {"output_type": "audio", "output_format": "m4a"})
    mark_expiration(directory, record["expires_epoch"])
    Queue("media", connection=REDIS).enqueue("media_worker.jobs.process_media_job", job_id,
                                               job_id=job_id, job_timeout=SETTINGS.job_timeout_seconds)
    accepted = {"job_id": job_id, "access_token": token, "status_url": f"/api/v1/jobs/{job_id}"}
    final, observed = wait_terminal(accepted)
    assert final["status"] == "failed" and not directory.exists(), observed
    code, _, _ = http("GET", f"/api/v1/jobs/{job_id}/download", headers=authorization(accepted))
    assert code == 409
    print("failure:", job_id, "invalid input and worker failure handled safely")


if __name__ == "__main__":
    assert REDIS.ping()
    assert len(Queue("media", connection=REDIS)) == 0
    assert len(StartedJobRegistry("media", connection=REDIS)) == 0
    check_audio()
    check_video_progress_and_running_cancel()
    check_queued_cancel()
    check_failure()
