"""Importable RQ media processing and expiry tasks."""

import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path

from redis import Redis
from rq import Queue

from media_core.engine import EngineConfig, MediaEngine
from media_core.errors import ErrorCode, MediaError
from media_core.formats import OUTPUT_FORMATS
from media_core.jobs import (JOB_ID_PATTERN, JobStore, cleanup_expired_job,
                             remove_storage_directory, safe_directory, update_expiration)
from media_core.models import MediaInfo, ProcessRequest
from media_core.names import safe_stem
from media_core.settings import Settings
from media_core.sources import LocalFileSource
from media_core.storage import sha256_file


log = logging.getLogger(__name__)
TRANSIENT = frozenset({ErrorCode.PROCESSING_TIMEOUT, ErrorCode.PROBE_FAILED, ErrorCode.INTERNAL_ERROR})
PUBLIC_ERRORS = {
    ErrorCode.INVALID_SOURCE: "The source is invalid",
    ErrorCode.UNAUTHORIZED_SOURCE: "The source is not authorized",
    ErrorCode.MEDIA_NOT_FOUND: "The source is unavailable",
    ErrorCode.INVALID_MEDIA: "The source is not valid media",
    ErrorCode.UNSUPPORTED_CODEC: "The selected codec is unsupported",
    ErrorCode.RESOLUTION_UNAVAILABLE: "The selected resolution is unavailable",
    ErrorCode.MISSING_AUDIO: "The source has no audio stream",
    ErrorCode.PROBE_FAILED: "Media inspection failed",
    ErrorCode.FFMPEG_FAILED: "Media processing failed",
    ErrorCode.PROCESSING_TIMEOUT: "Media processing timed out",
    ErrorCode.SIZE_LIMIT_EXCEEDED: "Media exceeds the size limit",
    ErrorCode.INSUFFICIENT_DISK_SPACE: "Storage is temporarily unavailable",
    ErrorCode.INTERNAL_ERROR: "Media processing failed",
}


class PublishedResultError(Exception):
    """The saved result cannot safely be exposed as a completed download."""


def process_media_job(job_id: str) -> None:
    settings = Settings()
    redis = Redis.from_url(settings.redis_url)
    store = JobStore(redis, settings)
    root = settings.media_storage_dir.resolve()
    job_dir = safe_directory(root, "jobs", job_id)
    lock = redis.lock(f"media:job:worker:{job_id}", timeout=settings.job_timeout_seconds + 60, blocking=False)
    if not lock.acquire(blocking=False):
        log.warning("Duplicate worker execution skipped", extra={"job_id": job_id})
        return
    try:
        record = store.get(job_id)
        if not record or record["status"] in ("completed", "failed", "cancelled"):
            return
        source_root = job_dir / "input"
        output_root = job_dir / "output"

        def remove_job_dir(expires_epoch: float) -> None:
            try:
                remove_storage_directory(job_dir, expires_epoch)
            except OSError:
                log.exception("Job storage cleanup deferred", extra={"job_id": job_id})

        def finish_cancelled(claim_id: str | None) -> None:
            cancelled_record = store.transition_owned(
                job_id, claim_id, "cancelled", cancel_requested=True, stage="cancelled",
                error={"code": "cancelled", "message": "Processing was cancelled"},
            )
            if cancelled_record:
                remove_job_dir(cancelled_record["expires_epoch"])

        existing_claim = record.get("claim_id")
        if record["status"] == "processing" and record["cancel_requested"]:
            finish_cancelled(existing_claim)
            return
        if record["status"] == "processing" and existing_claim and (
            manifest := _manifest_path(job_dir, existing_claim)
        ).is_file():
            try:
                if _complete_published(store, job_id, existing_claim, manifest, output_root):
                    shutil.rmtree(source_root, ignore_errors=True)
                else:
                    finish_cancelled(existing_claim)
            except PublishedResultError:
                # The prior worker published an unusable result. Keep any output
                # for expiry cleanup, but release the claim for another attempt.
                manifest.unlink(missing_ok=True)
                if record["attempts"] < settings.max_job_attempts:
                    retrying = store.transition_owned(
                        job_id, existing_claim, "queued", stage="recovering",
                        claim_id=None, claim_deadline=None, recovery_pending=True,
                    )
                    if retrying:
                        raise RuntimeError("Published media result is unavailable") from None
                    finish_cancelled(existing_claim)
                else:
                    store.transition_owned(
                        job_id, existing_claim, "failed", stage="failed",
                        error={"code": "internal_error", "message": "Media processing failed"},
                    )
            return
        claim_id = uuid.uuid4().hex
        record = store.claim(job_id, claim_id)
        if not record:
            return
        manifest = _manifest_path(job_dir, claim_id)
        if manifest.is_file():
            if _complete_published(store, job_id, claim_id, manifest, output_root):
                shutil.rmtree(source_root, ignore_errors=True)
            else:
                finish_cancelled(claim_id)
            return
        filename = record["source_filename"]
        last = {"time": 0.0, "percent": -1, "stage": ""}

        def cancelled() -> bool:
            current = store.get(job_id)
            return (current is None or current["status"] == "cancelled"
                    or current["cancel_requested"] or current.get("claim_id") != claim_id)

        def progress(percent: int | None, stage: str) -> None:
            stage = {"staging": "preparing", "inspecting": "preparing", "verifying": "finalizing"}.get(stage, stage)
            now = time.monotonic()
            value = percent if percent is not None else last["percent"]
            if stage != last["stage"] or value > last["percent"] and now - last["time"] >= 0.5:
                store.progress(job_id, percent, stage, claim_id=claim_id)
                last.update(time=now, percent=value, stage=stage)

        published = False
        try:
            if job_dir.is_symlink() or source_root.is_symlink() or output_root.is_symlink():
                raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "Job storage is unavailable")
            request = ProcessRequest(
                source_reference=filename, output_dir=output_root, **record["request"],
            )
            probe_timeout = min(20, max(1, settings.job_timeout_seconds // 6))
            shutdown_margin = min(30, max(1, settings.job_timeout_seconds // 10))
            engine = MediaEngine(EngineConfig(
                max_output_bytes=settings.max_output_bytes,
                probe_timeout_seconds=probe_timeout,
                process_timeout_seconds=max(1, settings.job_timeout_seconds - 2 * probe_timeout - shutdown_margin),
                total_timeout_seconds=max(1, settings.job_timeout_seconds - shutdown_margin),
            ))
            if cancelled():
                finish_cancelled(claim_id)
                return
            result = engine.process(
                LocalFileSource(source_root, max_bytes=settings.max_input_bytes),
                request, progress=progress, cancelled=cancelled,
            )
            if cancelled():
                finish_cancelled(claim_id)
                return
            path = Path(result.path)
            if not path.is_file() or not path.resolve().is_relative_to(output_root.resolve()):
                raise MediaError(ErrorCode.FFMPEG_FAILED, "Published output is unavailable")
            if path.stat().st_size != result.file_size or result.file_size <= 0:
                raise MediaError(ErrorCode.FFMPEG_FAILED, "Published output size does not match")
            published = True
            with path.open("rb") as output:
                checksum = sha256_file(output)
            _write_manifest(manifest, {
                "filename": path.name, "output_format": result.output_format,
                "file_size": result.file_size, "media": result.media.model_dump(),
                "sha256": checksum,
            })
            if not _complete_published(store, job_id, claim_id, manifest, output_root):
                finish_cancelled(claim_id)
                return
            shutil.rmtree(source_root, ignore_errors=True)
            log.info("Media job completed", extra={"job_id": job_id})
        except MediaError as error:
            if error.code == ErrorCode.CANCELLED or cancelled():
                finish_cancelled(claim_id)
                return
            if error.code in TRANSIENT and record["attempts"] < settings.max_job_attempts:
                retrying = store.transition_owned(
                    job_id, claim_id, "queued", stage="retrying", recovery_pending=True,
                )
                if not retrying:
                    finish_cancelled(claim_id)
                    return
                shutil.rmtree(output_root, ignore_errors=True)
                log.warning("Retrying transient media failure", extra={"job_id": job_id, "code": error.code.value})
                raise RuntimeError("Transient media processing failure") from None
            failed = store.transition_owned(
                job_id, claim_id, "failed", stage="failed",
                error={"code": error.code.value, "message": PUBLIC_ERRORS.get(error.code, "Media processing failed")},
            )
            if failed:
                remove_job_dir(failed["expires_epoch"])
            else:
                finish_cancelled(claim_id)
            log.warning("Media job failed", extra={"job_id": job_id, "code": error.code.value})
        except Exception:
            if published or manifest.is_file():
                log.exception("Published result awaits Redis completion", extra={"job_id": job_id})
                raise
            failed = store.transition_owned(
                job_id, claim_id, "failed", stage="failed",
                error={"code": "internal_error", "message": "Media processing failed"},
            )
            if failed:
                remove_job_dir(failed["expires_epoch"])
            else:
                finish_cancelled(claim_id)
            log.exception("Unexpected media job failure", extra={"job_id": job_id})
    finally:
        try:
            lock.release()
        except Exception:
            log.warning("Could not release job lock", extra={"job_id": job_id})


def _manifest_path(job_dir: Path, claim_id: str) -> Path:
    if not JOB_ID_PATTERN.fullmatch(claim_id):
        raise ValueError("Invalid worker claim")
    return job_dir / f".result-{claim_id}.json"


def _write_manifest(path: Path, result: dict) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".result-", delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(result, output)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _complete_published(store: JobStore, job_id: str, claim_id: str, manifest: Path, output_root: Path) -> bool:
    try:
        if manifest.parent.is_symlink() or manifest.is_symlink() or output_root.is_symlink():
            raise ValueError("Published storage is unsafe")
        result = json.loads(manifest.read_text())
        filename = result["filename"]
        size = result["file_size"]
        output_format = result["output_format"]
        media = MediaInfo.model_validate(result["media"])
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("Published filename is unsafe")
        if output_format not in OUTPUT_FORMATS or filename != f"{safe_stem(filename)}.{output_format}":
            raise ValueError("Published output format is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError("Published output size is invalid")
        if media.file_size != size:
            raise ValueError("Published media metadata does not match")
        if media.container != OUTPUT_FORMATS[output_format].container:
            raise ValueError("Published media container does not match")
        path = (output_root / filename).resolve()
        if not path.is_relative_to(output_root.resolve()) or not path.is_file() or (output_root / filename).is_symlink():
            raise ValueError("Published output is unavailable")
        if path.stat().st_size != size:
            raise ValueError("Published output size does not match")
        checksum = result.get("sha256")
        if checksum is not None:
            if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
                raise ValueError("Published checksum is invalid")
            with path.open("rb") as output:
                if sha256_file(output) != checksum:
                    raise ValueError("Published output checksum does not match")
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise PublishedResultError("Published result cannot be verified") from error
    completed = store.transition_owned(job_id, claim_id, "completed", stage="completed", progress=100, result=result)
    if completed:
        try:
            update_expiration(manifest.parent, completed["expires_epoch"])
        except OSError:
            log.exception("Could not update completed job expiry marker", extra={"job_id": job_id})
        # The task scheduled at submission uses the worst-case queue and retry
        # deadline. Completion starts a shorter retention window, so add its
        # cleanup now; the original task remains a fallback if this fails.
        try:
            delay = max(1, int(completed["expires_epoch"] - time.time()) + 1)
            scheduled = Queue("media", connection=store.redis).enqueue_in(
                timedelta(seconds=delay), "media_worker.jobs.cleanup_job", job_id,
            )
            if scheduled is None:
                raise RuntimeError("Completion cleanup was not scheduled")
        except Exception:
            log.exception("Could not schedule completion cleanup", extra={"job_id": job_id})
    return completed is not None


def cleanup_source(source_id: str) -> None:
    settings = Settings()
    redis = Redis.from_url(settings.redis_url)
    store = JobStore(redis, settings)
    root = settings.media_storage_dir.resolve()
    path = safe_directory(root, "sources", source_id)
    ttl = redis.ttl(store._source_key(source_id))
    if ttl > 0:
        scheduled = Queue("media", connection=redis).enqueue_in(
            timedelta(seconds=ttl + 1), cleanup_source, source_id,
        )
        if scheduled is None:
            raise RuntimeError("Source cleanup was not rescheduled")
        return
    remove_storage_directory(path, time.time())


def cleanup_job(job_id: str) -> None:
    settings = Settings()
    redis = Redis.from_url(settings.redis_url)
    root = settings.media_storage_dir.resolve()
    _, delay = cleanup_expired_job(root, redis, settings, job_id)
    if delay is not None:
        scheduled = Queue("media", connection=redis).enqueue_in(
            timedelta(seconds=max(1, int(delay) + 1)), cleanup_job, job_id,
        )
        if scheduled is None:
            raise RuntimeError("Job cleanup was not rescheduled")
