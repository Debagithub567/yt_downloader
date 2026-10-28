"""Worker lifecycle checks using local media and an in-memory Redis/RQ boundary."""

import importlib
import inspect
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from redis import WatchError

from media_api import routes
from media_core.engine import MediaEngine
import media_core.engine as engine_module
from media_core.errors import ErrorCode, MediaError
from media_core.jobs import JobStore
from media_core.settings import Settings
from media_core.storage import LocalResultStore
from media_worker import jobs, main as worker_main


class MemoryRedis:
    def __init__(self):
        self.records = {}
        self.versions = {}
        self.locks = set()
        self.guard = threading.RLock()
        self.fail_completion = False
        self.history = []

    def get(self, key):
        return self.records.get(key)

    def set(self, key, value, *, ex=None, nx=False):
        with self.guard:
            if nx and key in self.records:
                return False
            if self.fail_completion and key.startswith("media:job:") and json.loads(value)["status"] == "completed":
                raise ConnectionError("Redis completion failed")
            self.records[key] = value
            self.versions[key] = self.versions.get(key, 0) + 1
            if key.startswith("media:job:"):
                self.history.append(json.loads(value)["status"])
            return True

    def delete(self, key):
        self.records.pop(key, None)

    def ttl(self, key):
        return 3600 if key in self.records else -2

    def lock(self, name, **_kwargs):
        redis = self

        class Lock:
            def acquire(self, blocking=True):
                with redis.guard:
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

            def watch(self, key):
                self.key = key
                self.version = redis.versions.get(key, 0)

            def get(self, key):
                return redis.get(key)

            def multi(self):
                pass

            def set(self, key, value, *, ex=None):
                self.pending = (key, value, ex)

            def execute(self):
                with redis.guard:
                    if redis.versions.get(self.key, 0) != self.version:
                        raise WatchError()
                    redis.set(self.pending[0], self.pending[1], ex=self.pending[2])

        return Pipeline()


class MemoryQueue:
    def __init__(self):
        self.tasks = []

    def __len__(self):
        return 0

    def enqueue_in(self, _delay, function, job_id):
        self.tasks.append((function, job_id))
        return SimpleNamespace(id="scheduled")

    def enqueue(self, function, *args, **kwargs):
        self.tasks.append((function, args[0]))
        return SimpleNamespace(id=kwargs.get("job_id", args[0]))


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "0.25", "-c:a", "pcm_s16le", str(source)],
        check=True, timeout=15,
    )
    settings = Settings(media_storage_dir=tmp_path / "storage", max_job_attempts=2)
    redis = MemoryRedis()
    queue = MemoryQueue()
    monkeypatch.setattr(routes, "Queue", lambda *_args, **_kwargs: queue)
    monkeypatch.setattr(jobs.Redis, "from_url", lambda _url: redis)
    monkeypatch.setattr(jobs, "Settings", lambda: settings)
    client = TestClient(routes.create_app(redis_client=redis, settings=settings))
    uploaded = client.post("/api/v1/media/inspect", content=source.read_bytes(),
                           headers={"content-type": "application/octet-stream", "x-filename": source.name})
    assert uploaded.status_code == 201, uploaded.text
    submitted = client.post("/api/v1/jobs", json={
        "source_id": uploaded.json()["source_id"], "source_token": uploaded.json()["source_token"],
        "output_type": "audio", "output_format": "m4a",
    })
    assert submitted.status_code == 202, submitted.text
    return SimpleNamespace(settings=settings, redis=redis, queue=queue, client=client,
                           accepted=submitted.json(), store=JobStore(redis, settings))


def record(context):
    return context.store.get(context.accepted["job_id"])


def job_dir(context):
    return context.settings.media_storage_dir / "jobs" / context.accepted["job_id"]


def run_job(context):
    jobs.process_media_job(context.accepted["job_id"])


def test_rq_entrypoints_match_api_task_arguments(context):
    assert worker_main.CleanupWorker.__module__ == "media_worker.main"
    for function, job_id in context.queue.tasks:
        module, name = function.rsplit(".", 1)
        target = getattr(importlib.import_module(module), name)
        assert list(inspect.signature(target).parameters) == ["job_id" if name == "process_media_job"
                                                               else "source_id" if name == "cleanup_source" else "job_id"]
        assert isinstance(job_id, str)
    assert context.queue.tasks[-1] == ("media_worker.jobs.process_media_job", context.accepted["job_id"])


def test_success_publishes_before_completed_and_status_is_safe(context):
    run_job(context)
    current = record(context)
    assert context.redis.history[0] == "queued"
    assert "processing" in context.redis.history[1:-1]
    assert context.redis.history[-1] == "completed"
    assert current["status"] == "completed" and current["progress"] == 100
    output = job_dir(context) / "output" / current["result"]["filename"]
    assert output.is_file() and output.stat().st_size == current["result"]["file_size"]
    assert current["result"]["media"]["audio_streams"][0]["codec"] == "aac"
    assert not list((job_dir(context) / "output").glob(".media-job-*"))
    assert not (job_dir(context) / "input").exists()
    response = context.client.get(context.accepted["status_url"], headers={
        "authorization": f"Bearer {context.accepted['access_token']}"})
    assert response.status_code == 200
    assert response.json()["output"]["size_bytes"] == output.stat().st_size
    assert str(context.settings.media_storage_dir) not in response.text


def test_real_ffmpeg_progress_precedes_completion(context, monkeypatch):
    observed = []
    original = MediaEngine.process

    def captured(engine, source, request, *, progress, cancelled):
        def collect(percent, stage):
            observed.append((percent, stage))
            progress(percent, stage)
        return original(engine, source, request, progress=collect, cancelled=cancelled)

    monkeypatch.setattr(MediaEngine, "process", captured)
    run_job(context)
    numeric = [value for value, stage in observed if stage == "processing" and value is not None]
    assert numeric and any(value > 0 for value in numeric)
    assert all(0 <= value <= 99 for value in numeric)
    assert any(stage == "finalizing" for _, stage in observed)
    assert record(context)["status"] == "completed" and record(context)["progress"] == 100


def test_missing_record_and_terminal_duplicate_are_noops(context, monkeypatch):
    jobs.process_media_job("f" * 32)
    run_job(context)
    before = record(context)
    monkeypatch.setattr(MediaEngine, "process", lambda *_args, **_kwargs: pytest.fail("duplicate processed"))
    run_job(context)
    assert record(context) == before


def test_processing_without_a_saved_result_is_not_claimed_twice(context, monkeypatch):
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "a" * 32)
    before = record(context)
    monkeypatch.setattr(MediaEngine, "process", lambda *_args, **_kwargs: pytest.fail("job was reprocessed"))
    run_job(context)
    assert record(context) == before


def test_two_claims_cannot_both_start_or_finish(context):
    job_id = context.accepted["job_id"]
    first = context.store.claim(job_id, "first")
    assert first and first["status"] == "processing"
    assert context.store.claim(job_id, "second") is None
    assert context.store.transition_owned(job_id, "second", "completed", result={}) is None
    assert record(context)["status"] == "processing"
    context.store.transition_owned(job_id, "first", "queued", claim_id=None, recovery_pending=True)
    assert context.store.claim(job_id, "second")
    assert context.store.transition_owned(job_id, "first", "completed", result={}) is None
    assert record(context)["claim_id"] == "second"


def test_concurrent_claims_have_one_owner(context):
    job_id = context.accepted["job_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda claim: context.store.claim(job_id, claim), ("first", "second")))
    assert sum(claim is not None for claim in claims) == 1
    assert record(context)["attempts"] == 1


def test_duplicate_worker_lock_does_not_claim(context):
    lock = context.redis.lock(f"media:job:worker:{context.accepted['job_id']}")
    assert lock.acquire(blocking=False)
    try:
        run_job(context)
        assert record(context)["status"] == "queued"
    finally:
        lock.release()


def test_missing_staged_source_fails_safely(context):
    (job_dir(context) / "input" / "tone.wav").unlink()
    run_job(context)
    assert record(context)["status"] == "failed"
    assert record(context)["error"]["code"] == "media_not_found"
    assert not job_dir(context).exists()


@pytest.mark.parametrize("code", [ErrorCode.INVALID_MEDIA, ErrorCode.UNSUPPORTED_CODEC, ErrorCode.FFMPEG_FAILED])
def test_media_errors_fail_without_trace_or_output(context, monkeypatch, code):
    def fail(*_args, **_kwargs):
        raise MediaError(code, "private filesystem path")

    monkeypatch.setattr(MediaEngine, "process", fail)
    run_job(context)
    assert record(context)["status"] == "failed"
    assert "private" not in json.dumps(record(context)["error"])
    assert not job_dir(context).exists()


def test_unexpected_worker_error_is_recorded_safely(context, monkeypatch):
    monkeypatch.setattr(MediaEngine, "process", lambda *_args, **_kwargs: 1 / 0)
    run_job(context)
    assert record(context)["status"] == "failed"
    assert record(context)["error"]["code"] == "internal_error"
    assert not job_dir(context).exists()


def test_timeout_retries_with_explicit_attempt_progress(context, monkeypatch):
    def timeout(*_args, progress, **_kwargs):
        progress(73, "processing")
        raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "timed out")

    monkeypatch.setattr(MediaEngine, "process", timeout)
    with pytest.raises(RuntimeError, match="Transient media"):
        run_job(context)
    assert record(context)["status"] == "queued"
    assert record(context)["progress"] is None
    assert record(context)["last_attempt_progress"] == 73
    assert record(context)["attempts"] == 1
    assert record(context)["recovery_pending"] is True
    current = context.store.claim(context.accepted["job_id"], "a" * 32)
    assert current["attempts"] == 2 and current["progress"] == 0


def test_progress_is_monotonic_within_attempt_and_unknown_is_explicit(context):
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "a" * 32)
    context.store.progress(job_id, 48, "processing", claim_id="a" * 32)
    context.store.progress(job_id, 21, "processing", claim_id="a" * 32)
    assert record(context)["progress"] == 48
    context.store.transition_owned(job_id, "a" * 32, "queued", stage="retrying")
    assert record(context)["progress"] is None
    assert record(context)["last_attempt_progress"] == 48
    context.store.claim(job_id, "b" * 32)
    context.store.progress(job_id, None, "processing", claim_id="b" * 32)
    assert record(context)["progress"] is None
    assert record(context)["attempts"] == 2
    status = context.client.get(context.accepted["status_url"], headers={
        "authorization": f"Bearer {context.accepted['access_token']}"})
    assert status.json()["attempt"] == 2
    assert status.json()["last_attempt_progress"] == 48
    assert "progress" not in status.json()


def test_worker_throttles_progress_writes_and_never_reports_100_early(context, monkeypatch):
    def processing(*_args, progress, **_kwargs):
        for percent in range(1, 50):
            progress(percent, "processing")
        progress(None, "finalizing")
        assert record(context)["progress"] < 100
        raise MediaError(ErrorCode.CANCELLED, "cancelled")

    monkeypatch.setattr(MediaEngine, "process", processing)
    run_job(context)
    # Initial queue, claim, one numeric update, stage change, cancellation.
    assert context.redis.history.count("processing") == 3
    assert record(context)["status"] == "cancelled"


def test_timeout_on_last_attempt_fails(context, monkeypatch):
    def timeout(*_args, **_kwargs):
        raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "private timeout detail")

    monkeypatch.setattr(MediaEngine, "process", timeout)
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "previous")
    context.store.transition_owned(job_id, "previous", "queued", claim_id=None)
    run_job(context)
    assert record(context)["status"] == "failed"
    assert record(context)["error"] == {
        "code": "processing_timeout", "message": "Media processing timed out",
    }


def test_output_publication_failure_does_not_complete(context, monkeypatch):
    monkeypatch.setattr(LocalResultStore, "publish", lambda *_args: (_ for _ in ()).throw(OSError("disk failed")))
    with pytest.raises(RuntimeError, match="Transient media"):
        run_job(context)
    assert record(context)["status"] == "queued"
    assert not (job_dir(context) / "output").exists()


def test_completion_redis_failure_preserves_output_and_reuses_it(context, monkeypatch):
    context.redis.fail_completion = True
    with pytest.raises(ConnectionError, match="completion failed"):
        run_job(context)
    assert record(context)["status"] == "processing"
    manifest = job_dir(context) / f".result-{record(context)['claim_id']}.json"
    assert manifest.is_file()
    output = job_dir(context) / "output" / json.loads(manifest.read_text())["filename"]
    assert output.is_file()
    context.redis.fail_completion = False
    monkeypatch.setattr(MediaEngine, "process", lambda *_args, **_kwargs: pytest.fail("reprocessed output"))
    run_job(context)
    assert record(context)["status"] == "completed"
    assert output.is_file()


def test_completed_transition_observes_published_file(context, monkeypatch):
    original = JobStore.transition_owned

    def checked(store, job_id, claim_id, target, **fields):
        if target == "completed":
            result = fields["result"]
            output = job_dir(context) / "output" / result["filename"]
            assert output.is_file()
            assert output.stat().st_size == result["file_size"]
            assert (job_dir(context) / f".result-{claim_id}.json").is_file()
        return original(store, job_id, claim_id, target, **fields)

    monkeypatch.setattr(JobStore, "transition_owned", checked)
    run_job(context)
    assert record(context)["status"] == "completed"


def test_manifest_write_failure_preserves_published_output(context, monkeypatch):
    monkeypatch.setattr(jobs, "_write_manifest", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        run_job(context)
    assert record(context)["status"] == "processing"
    assert list((job_dir(context) / "output").glob("*.m4a"))


@pytest.mark.parametrize("manifest", [
    {"filename": "missing.m4a", "output_format": "m4a", "file_size": 10, "media": {"title": "lost"}},
    {"filename": "../unsafe.m4a", "output_format": "m4a", "file_size": 3, "media": {"title": "bad"}},
    {"filename": "bad.m4a", "output_format": "m4a", "file_size": 3, "media": {}},
])
def test_invalid_saved_result_retries_without_false_completion(context, manifest):
    job_id = context.accepted["job_id"]
    claim_id = "a" * 32
    context.store.claim(job_id, claim_id)
    saved = job_dir(context) / f".result-{claim_id}.json"
    saved.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="Published media result"):
        run_job(context)
    assert record(context)["status"] == "queued"
    assert record(context)["progress"] is None
    assert not saved.exists()
    run_job(context)
    assert record(context)["status"] == "completed"


def test_invalid_saved_result_on_last_attempt_fails(context):
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "a" * 32)
    context.store.transition_owned(job_id, "a" * 32, "queued", claim_id=None)
    context.store.claim(job_id, "b" * 32)
    (job_dir(context) / f".result-{'b' * 32}.json").write_text("not json")
    run_job(context)
    assert record(context)["status"] == "failed"
    assert record(context)["error"]["code"] == "internal_error"
    assert record(context)["result"] is None


def test_stale_claim_manifest_cannot_replace_new_attempt(context):
    job_id = context.accepted["job_id"]
    stale_claim = "a" * 32
    context.store.claim(job_id, stale_claim)
    old_output = job_dir(context) / "output" / "old.m4a"
    old_output.parent.mkdir(parents=True, exist_ok=True)
    old_output.write_bytes(b"old")
    (job_dir(context) / f".result-{stale_claim}.json").write_text(json.dumps({
        "filename": old_output.name, "output_format": "m4a", "file_size": 3,
        "media": {"title": "old"},
    }))
    context.store.transition_owned(job_id, stale_claim, "queued", claim_id=None, recovery_pending=True)
    run_job(context)
    current = record(context)
    assert current["status"] == "completed"
    assert current["result"]["filename"] != old_output.name
    assert current["result"]["media"]["audio_streams"][0]["codec"] == "aac"


def test_abandoned_claim_is_requeued_and_reclaimed(context, monkeypatch):
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "dead-worker")
    context.store.mutate(job_id, lambda current: {**current, "claim_deadline": 0})
    monkeypatch.setattr(worker_main, "Queue", lambda *_args, **_kwargs: context.queue)
    worker_main.recover_abandoned_jobs(context.settings, context.redis)
    assert record(context)["status"] == "queued"
    assert context.queue.tasks[-1] == ("media_worker.jobs.process_media_job", job_id)
    run_job(context)
    assert record(context)["status"] == "completed"


def test_abandoned_published_result_completes_without_reprocessing(context, monkeypatch):
    context.redis.fail_completion = True
    with pytest.raises(ConnectionError, match="completion failed"):
        run_job(context)
    job_id = context.accepted["job_id"]
    context.store.mutate(job_id, lambda current: {**current, "claim_deadline": 0})
    monkeypatch.setattr(worker_main, "Queue", lambda *_args, **_kwargs: context.queue)
    worker_main.recover_abandoned_jobs(context.settings, context.redis)
    assert context.queue.tasks[-1] == ("media_worker.jobs.process_media_job", job_id)
    context.redis.fail_completion = False
    monkeypatch.setattr(MediaEngine, "process", lambda *_args, **_kwargs: pytest.fail("job was reprocessed"))
    run_job(context)
    assert record(context)["status"] == "completed"


def test_cancelled_job_does_not_restart(context):
    context.store.cancel(context.accepted["job_id"])
    run_job(context)
    assert record(context)["status"] == "cancelled"


def test_queued_cancellation_removes_files_and_is_idempotent(context, monkeypatch):
    monkeypatch.setattr(routes.Job, "fetch", lambda *_args, **_kwargs: MagicMock())
    url = context.accepted["status_url"] + "/cancel"
    headers = {"authorization": f"Bearer {context.accepted['access_token']}"}
    first = context.client.post(url, headers=headers)
    assert first.status_code == 200 and first.json()["status"] == "cancelled"
    assert not job_dir(context).exists()
    second = context.client.post(url, headers=headers)
    assert second.status_code == 200 and second.json()["status"] == "cancelled"
    run_job(context)
    assert record(context)["attempts"] == 0


def test_claim_winning_queued_cancellation_keeps_worker_directory(context, monkeypatch):
    original = JobStore.cancel

    def claim_before_cancel(store, job_id):
        context.store.claim(job_id, "a" * 32)
        return original(store, job_id)

    monkeypatch.setattr(JobStore, "cancel", claim_before_cancel)
    headers = {"authorization": f"Bearer {context.accepted['access_token']}"}
    response = context.client.post(context.accepted["status_url"] + "/cancel", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    assert response.json()["stage"] == "cancelling"
    assert job_dir(context).is_dir()
    monkeypatch.setattr(MediaEngine, "process", lambda *_args, **_kwargs: pytest.fail("cancelled source opened"))
    run_job(context)
    assert record(context)["status"] == "cancelled"
    assert not job_dir(context).exists()


def test_running_cancellation_stops_before_completion(context, monkeypatch):
    def processing(*_args, progress, cancelled, **_kwargs):
        progress(37, "processing")
        current = context.store.cancel(context.accepted["job_id"])
        assert current["status"] == "processing" and current["cancel_requested"]
        assert cancelled()
        raise MediaError(ErrorCode.CANCELLED, "cancelled")

    monkeypatch.setattr(MediaEngine, "process", processing)
    run_job(context)
    assert record(context)["status"] == "cancelled"
    assert record(context)["progress"] == 37
    assert not job_dir(context).exists()


def test_real_ffmpeg_cancellation_cleans_temporary_output(context, monkeypatch):
    original = engine_module.run_progress_command

    def cancel_on_progress(args, *, timeout, on_progress, should_cancel):
        def observed(seconds):
            context.store.cancel(context.accepted["job_id"])
            on_progress(seconds)
        return original(args, timeout=timeout, on_progress=observed, should_cancel=should_cancel)

    monkeypatch.setattr(engine_module, "run_progress_command", cancel_on_progress)
    run_job(context)
    assert record(context)["status"] == "cancelled"
    assert record(context)["result"] is None
    assert not job_dir(context).exists()


def test_cancellation_during_output_validation_prevents_publication(context, monkeypatch):
    original = engine_module.inspect_file

    def inspect(path, **kwargs):
        media = original(path, **kwargs)
        if path.name == "output.partial":
            context.store.cancel(context.accepted["job_id"])
        return media

    monkeypatch.setattr(engine_module, "inspect_file", inspect)
    run_job(context)
    assert record(context)["status"] == "cancelled"
    assert record(context)["result"] is None
    assert not job_dir(context).exists()


def test_cancellation_during_publication_discards_output(context, monkeypatch):
    original = LocalResultStore.publish

    def publish(store, partial, output_dir, filename):
        path = original(store, partial, output_dir, filename)
        context.store.cancel(context.accepted["job_id"])
        return path

    monkeypatch.setattr(LocalResultStore, "publish", publish)
    run_job(context)
    assert record(context)["status"] == "cancelled"
    assert record(context)["result"] is None
    assert not job_dir(context).exists()


def test_cancellation_wins_atomic_completion_race(context, monkeypatch):
    original = JobStore.transition_owned

    def cancel_first(store, job_id, claim_id, target, **fields):
        if target == "completed":
            context.store.cancel(job_id)
        return original(store, job_id, claim_id, target, **fields)

    monkeypatch.setattr(JobStore, "transition_owned", cancel_first)
    run_job(context)
    assert record(context)["status"] == "cancelled"
    assert not job_dir(context).exists()


def test_completion_winning_race_remains_completed(context):
    run_job(context)
    assert context.store.cancel(context.accepted["job_id"]) is None
    assert record(context)["status"] == "completed"
    assert job_dir(context).is_dir()


def test_abandoned_cancellation_is_recovered_by_worker(context, monkeypatch):
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "a" * 32)
    context.store.cancel(job_id)
    context.store.mutate(job_id, lambda current: {**current, "claim_deadline": 0})
    monkeypatch.setattr(worker_main, "Queue", lambda *_args, **_kwargs: context.queue)
    worker_main.recover_abandoned_jobs(context.settings, context.redis)
    assert context.queue.tasks[-1] == ("media_worker.jobs.process_media_job", job_id)
    run_job(context)
    assert record(context)["status"] == "cancelled"
    assert not job_dir(context).exists()
