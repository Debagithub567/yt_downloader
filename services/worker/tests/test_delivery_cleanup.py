"""Delivery and expiry checks using the worker's local API/RQ fixture."""

import time
from pathlib import Path
from urllib.parse import quote

import pytest
from unittest.mock import MagicMock

from media_api import routes
from media_api.routes import _open_published_output
from media_core.jobs import cleanup_expired_job, sweep_expired_storage
from media_worker import jobs
import media_core.jobs as job_storage

from test_jobs import context, job_dir, record, run_job


def token_headers(context):
    return {"authorization": f"Bearer {context.accepted['access_token']}"}


def download(context, **kwargs):
    return context.client.get(context.accepted["status_url"] + "/download",
                              headers=token_headers(context), **kwargs)


def change_result(context, **fields):
    job_id = context.accepted["job_id"]
    return context.store.mutate(job_id, lambda current: {
        **current, "result": {**current["result"], **fields},
    })


def expire(context):
    job_id = context.accepted["job_id"]
    context.store.mutate(job_id, lambda current: {**current, "expires_epoch": 0})
    (job_dir(context) / ".expires").write_text("0")


def test_completed_download_is_authorized_and_has_safe_headers(context):
    run_job(context)
    output = job_dir(context) / "output" / record(context)["result"]["filename"]
    response = download(context)
    assert response.status_code == 200
    assert response.content == output.read_bytes()
    assert response.headers["content-type"] == "audio/mp4"
    assert response.headers["content-length"] == str(output.stat().st_size)
    assert response.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''" + quote(output.name, safe=""))
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert str(context.settings.media_storage_dir) not in response.text[:100]
    assert float((job_dir(context) / ".expires").read_text()) == pytest.approx(record(context)["expires_epoch"])


def test_missing_invalid_and_unknown_capabilities_cannot_download(context, caplog):
    run_job(context)
    url = context.accepted["status_url"] + "/download"
    assert context.client.get(url).status_code == 401
    assert context.client.get(url, headers={"authorization": "Bearer wrong"}).status_code == 404
    assert context.client.get(url + "?access_token=" + context.accepted["access_token"]).status_code == 401
    assert context.client.get("/api/v1/jobs/" + "f" * 32 + "/download",
                              headers=token_headers(context)).status_code == 404
    assert context.accepted["access_token"] not in caplog.text


@pytest.mark.parametrize("status", ["queued", "processing", "failed", "cancelled"])
def test_noncompleted_jobs_cannot_download(context, status):
    context.store.mutate(context.accepted["job_id"], lambda current: {**current, "status": status})
    response = download(context)
    assert response.status_code == 409
    assert str(context.settings.media_storage_dir) not in response.text


def test_expired_job_cannot_download(context):
    run_job(context)
    expire(context)
    assert download(context).status_code == 410


def test_expired_queued_job_cannot_start_processing(context):
    expire(context)
    job_id = context.accepted["job_id"]
    assert context.store.claim(job_id, "a" * 32) is None
    run_job(context)
    assert record(context)["status"] == "queued"


def test_missing_or_size_mismatched_output_is_not_delivered(context):
    run_job(context)
    output = job_dir(context) / "output" / record(context)["result"]["filename"]
    original = output.read_bytes()
    output.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    assert download(context).status_code == 410
    output.write_bytes(b"corrupt")
    assert download(context).status_code == 410
    output.unlink()
    assert download(context).status_code == 410


@pytest.mark.parametrize("filename", ["../other.m4a", "/etc/passwd", "bad\r\nX-Test: yes.m4a"])
def test_untrusted_result_filename_is_rejected(context, filename):
    run_job(context)
    change_result(context, filename=filename)
    response = download(context)
    assert response.status_code == 410
    assert "passwd" not in response.text


def test_symlink_output_and_parent_escape_are_rejected(context, tmp_path):
    run_job(context)
    output = job_dir(context) / "output" / record(context)["result"]["filename"]
    outside = tmp_path / "outside.m4a"
    outside.write_bytes(output.read_bytes())
    output.unlink()
    output.symlink_to(outside)
    assert download(context).status_code == 410
    output.unlink()
    output.parent.rmdir()
    output.parent.symlink_to(tmp_path, target_is_directory=True)
    assert download(context).status_code == 410
    assert outside.is_file()


@pytest.mark.parametrize("component", ["input", "output"])
def test_worker_rejects_symlinked_job_storage(context, tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    target = job_dir(context) / component
    if target.exists():
        job_storage.shutil.rmtree(target)
    target.symlink_to(outside, target_is_directory=True)
    run_job(context)
    assert record(context)["status"] == "failed"
    assert record(context)["error"]["code"] == "unauthorized_source"
    assert (outside / "keep").read_text() == "safe"


def test_invalid_result_metadata_and_manifest_are_safe(context):
    run_job(context)
    valid_result = record(context)["result"]
    manifest = next(job_dir(context).glob(".result-*.json"))
    manifest.write_text("not json")
    # The completed Redis result is authoritative; the manifest is for recovery.
    assert download(context).status_code == 200
    change_result(context, media={"title": "missing size"})
    assert download(context).status_code == 410
    change_result(context, output_format="unknown")
    assert download(context).status_code == 410
    change_result(context, media=valid_result["media"], output_format="m4a", sha256="0" * 64)
    assert download(context).status_code == 410


def test_result_cannot_select_another_job_directory(context, tmp_path):
    run_job(context)
    filename = record(context)["result"]["filename"]
    output = job_dir(context) / "output" / filename
    other = context.settings.media_storage_dir / "jobs" / ("e" * 32) / "output"
    other.mkdir(parents=True)
    (other / filename).write_bytes(output.read_bytes())
    output.unlink()
    assert download(context).status_code == 410
    assert (other / filename).is_file()


def test_open_download_survives_expiry_cleanup(context):
    run_job(context)
    job_id = context.accepted["job_id"]
    expected = (job_dir(context) / "output" / record(context)["result"]["filename"]).read_bytes()
    opened, _, _, _ = _open_published_output(context.settings.media_storage_dir.resolve(),
                                             job_id, record(context)["result"])
    try:
        expire(context)
        deleted, delay = cleanup_expired_job(context.settings.media_storage_dir.resolve(),
                                            context.redis, context.settings, job_id)
        assert deleted and delay is None
        assert opened.read() == expected
        assert not job_dir(context).exists()
    finally:
        opened.close()


def test_expired_cleanup_is_idempotent_and_keeps_other_jobs(context, monkeypatch):
    run_job(context)
    other = context.settings.media_storage_dir / "jobs" / ("e" * 32)
    other.mkdir(parents=True)
    (other / ".expires").write_text(str(time.time() + 3600))
    expire(context)
    monkeypatch.setattr(jobs.Redis, "from_url", lambda _url: context.redis)
    monkeypatch.setattr(jobs, "Settings", lambda: context.settings)
    jobs.cleanup_job(context.accepted["job_id"])
    jobs.cleanup_job(context.accepted["job_id"])
    assert not job_dir(context).exists()
    assert record(context) is None
    assert other.is_dir()


def test_cleanup_waits_for_worker_lock_and_processing_claim(context, monkeypatch):
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "a" * 32)
    expire(context)
    monkeypatch.setattr(jobs.Redis, "from_url", lambda _url: context.redis)
    monkeypatch.setattr(jobs, "Settings", lambda: context.settings)
    monkeypatch.setattr(jobs, "Queue", lambda *_args, **_kwargs: context.queue)
    lock = context.redis.lock(f"media:job:worker:{job_id}")
    assert lock.acquire(blocking=False)
    try:
        jobs.cleanup_job(job_id)
        assert job_dir(context).is_dir()
        assert context.queue.tasks[-1] == (jobs.cleanup_job, job_id)
    finally:
        lock.release()
    jobs.cleanup_job(job_id)
    assert job_dir(context).is_dir()
    context.store.mutate(job_id, lambda current: {**current, "claim_deadline": 0})
    jobs.cleanup_job(job_id)
    assert not job_dir(context).exists()


def test_cleanup_during_running_cancellation_defers_to_worker(context, monkeypatch):
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "a" * 32)
    context.store.cancel(job_id)
    expire(context)
    monkeypatch.setattr(jobs.Redis, "from_url", lambda _url: context.redis)
    monkeypatch.setattr(jobs, "Settings", lambda: context.settings)
    monkeypatch.setattr(jobs, "Queue", lambda *_args, **_kwargs: context.queue)
    jobs.cleanup_job(job_id)
    assert job_dir(context).is_dir()
    run_job(context)
    assert record(context)["status"] == "cancelled"
    assert not job_dir(context).exists()


def test_cancelled_worker_cleanup_failure_keeps_recovery_marker(context, monkeypatch):
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "a" * 32)
    context.store.cancel(job_id)
    directory = job_dir(context)
    original = job_storage.shutil.rmtree

    def fail(path, *args, **kwargs):
        if Path(path) == directory:
            (directory / ".expires").unlink()
            raise OSError("filesystem busy")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(job_storage.shutil, "rmtree", fail)
    run_job(context)
    assert record(context)["status"] == "cancelled"
    assert (directory / ".expires").is_file()
    monkeypatch.setattr(job_storage.shutil, "rmtree", original)
    expire(context)
    assert sweep_expired_storage(context.settings.media_storage_dir.resolve(), context.redis,
                                 now=time.time(), settings=context.settings) == 1
    assert not directory.exists()


def test_failed_directory_cleanup_keeps_marker_for_recovery(context, monkeypatch):
    run_job(context)
    expire(context)
    directory = job_dir(context)
    original = job_storage.shutil.rmtree

    def partial(path, *args, **kwargs):
        if Path(path) == directory:
            (directory / ".expires").unlink()
            raise OSError("temporary filesystem failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(job_storage.shutil, "rmtree", partial)
    with pytest.raises(OSError, match="temporary filesystem failure"):
        cleanup_expired_job(context.settings.media_storage_dir.resolve(), context.redis,
                            context.settings, context.accepted["job_id"])
    assert (directory / ".expires").is_file()
    assert record(context) is not None
    monkeypatch.setattr(job_storage.shutil, "rmtree", original)
    assert sweep_expired_storage(context.settings.media_storage_dir.resolve(), context.redis,
                                 now=time.time(), settings=context.settings) == 1
    assert not directory.exists()


def test_crash_after_expiry_marker_removed_recovers_from_deletion_intent(context, monkeypatch):
    run_job(context)
    expire(context)
    directory = job_dir(context)
    intent = directory.parent / f".deleting-{directory.name}"
    original = job_storage.shutil.rmtree

    def interrupted(path, *args, **kwargs):
        if Path(path) == directory:
            (directory / ".expires").unlink()
            raise KeyboardInterrupt("worker stopped during deletion")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(job_storage.shutil, "rmtree", interrupted)
    with pytest.raises(KeyboardInterrupt, match="worker stopped"):
        cleanup_expired_job(context.settings.media_storage_dir.resolve(), context.redis,
                            context.settings, context.accepted["job_id"])
    assert directory.is_dir() and not (directory / ".expires").exists()
    assert intent.read_text() == directory.name
    monkeypatch.setattr(job_storage.shutil, "rmtree", original)
    assert sweep_expired_storage(context.settings.media_storage_dir.resolve(), context.redis,
                                 now=time.time(), settings=context.settings) == 1
    assert not directory.exists() and not intent.exists()
    assert record(context) is None


@pytest.mark.parametrize("intent_contents", [None, "invalid"])
def test_markerless_directory_without_valid_intent_is_preserved(context, intent_contents):
    run_job(context)
    expire(context)
    directory = job_dir(context)
    (directory / ".expires").unlink()
    intent = directory.parent / f".deleting-{directory.name}"
    if intent_contents is not None:
        intent.write_text(intent_contents)
    assert sweep_expired_storage(context.settings.media_storage_dir.resolve(), context.redis,
                                 now=time.time(), settings=context.settings) == 0
    assert directory.is_dir() and record(context) is not None


def test_deletion_intent_does_not_bypass_active_worker_ownership(context):
    job_id = context.accepted["job_id"]
    context.store.claim(job_id, "a" * 32)
    expire(context)
    directory = job_dir(context)
    (directory / ".expires").unlink()
    (directory.parent / f".deleting-{job_id}").write_text(job_id)
    root = context.settings.media_storage_dir.resolve()
    lock = context.redis.lock(f"media:job:worker:{job_id}")
    assert lock.acquire(blocking=False)
    try:
        assert sweep_expired_storage(root, context.redis, now=time.time(), settings=context.settings) == 0
        assert directory.is_dir()
    finally:
        lock.release()
    assert sweep_expired_storage(root, context.redis, now=time.time(), settings=context.settings) == 0
    assert directory.is_dir()
    context.store.mutate(job_id, lambda current: {**current, "claim_deadline": 0})
    assert sweep_expired_storage(root, context.redis, now=time.time(), settings=context.settings) == 1
    assert not directory.exists()


def test_queued_cancellation_failure_recovers_even_without_expiry_marker(context, monkeypatch):
    directory = job_dir(context)
    intent = directory.parent / f".deleting-{directory.name}"
    original = job_storage.shutil.rmtree

    def partial(path, *args, **kwargs):
        if Path(path) == directory:
            (directory / ".expires").unlink()
            raise OSError("temporary cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(routes.Job, "fetch", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(job_storage.shutil, "rmtree", partial)
    response = context.client.post(context.accepted["status_url"] + "/cancel", headers=token_headers(context))
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert intent.read_text() == directory.name
    assert (directory / ".expires").is_file()
    (directory / ".expires").unlink()
    monkeypatch.setattr(job_storage.shutil, "rmtree", original)
    assert sweep_expired_storage(context.settings.media_storage_dir.resolve(), context.redis,
                                 now=time.time(), settings=context.settings) == 1
    assert not directory.exists() and not intent.exists()
    assert record(context) is None


def test_redis_delete_failure_can_retry_after_files_are_removed(context, monkeypatch):
    run_job(context)
    expire(context)
    original = context.redis.delete
    monkeypatch.setattr(context.redis, "delete", lambda _key: (_ for _ in ()).throw(ConnectionError("Redis down")))
    with pytest.raises(ConnectionError, match="Redis down"):
        cleanup_expired_job(context.settings.media_storage_dir.resolve(), context.redis,
                            context.settings, context.accepted["job_id"])
    assert not job_dir(context).exists()
    assert record(context) is not None
    monkeypatch.setattr(context.redis, "delete", original)
    deleted, _ = cleanup_expired_job(context.settings.media_storage_dir.resolve(), context.redis,
                                     context.settings, context.accepted["job_id"])
    assert deleted and record(context) is None


def test_missed_cleanup_and_expired_record_recover_from_marker(context):
    run_job(context)
    expire(context)
    root = context.settings.media_storage_dir.resolve()
    assert sweep_expired_storage(root, context.redis, now=time.time(), settings=context.settings) == 1
    assert not job_dir(context).exists()
    assert record(context) is None


def test_missing_redis_record_does_not_leak_expired_storage(context):
    run_job(context)
    expire(context)
    context.redis.delete(f"media:job:{context.accepted['job_id']}")
    assert sweep_expired_storage(context.settings.media_storage_dir.resolve(), context.redis,
                                 now=time.time(), settings=context.settings) == 1
    assert not job_dir(context).exists()


def test_source_cleanup_reschedules_until_expired_then_is_idempotent(context, monkeypatch):
    source_id = record(context)["source_id"]
    source_dir = context.settings.media_storage_dir / "sources" / source_id
    monkeypatch.setattr(jobs.Redis, "from_url", lambda _url: context.redis)
    monkeypatch.setattr(jobs, "Settings", lambda: context.settings)
    monkeypatch.setattr(jobs, "Queue", lambda *_args, **_kwargs: context.queue)
    jobs.cleanup_source(source_id)
    assert source_dir.is_dir()
    assert context.queue.tasks[-1] == (jobs.cleanup_source, source_id)
    context.redis.delete(f"media:source:{source_id}")
    jobs.cleanup_source(source_id)
    jobs.cleanup_source(source_id)
    assert not source_dir.exists()


def test_cleanup_rejects_invalid_ids_and_does_not_follow_category_symlink(context, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    root = context.settings.media_storage_dir.resolve()
    with pytest.raises(ValueError):
        cleanup_expired_job(root, context.redis, context.settings, "../outside")
    jobs_root = root / "jobs"
    moved = root / "jobs-moved"
    jobs_root.rename(moved)
    jobs_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        cleanup_expired_job(root, context.redis, context.settings, context.accepted["job_id"])
    assert sweep_expired_storage(root, context.redis, now=time.time(), settings=context.settings) == 0
    assert (outside / "keep").read_text() == "safe"
