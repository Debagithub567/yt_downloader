"""FastAPI media inspection, RQ submission, status, cancellation, and delivery."""

import json
import logging
import os
import shutil
import stat
import time
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis import Redis
from rq import Queue, Retry
from rq.job import Job
from starlette.background import BackgroundTask

from media_core.engine import EngineConfig, MediaEngine, video_options
from media_core.errors import ErrorCode, MediaError
from media_core.formats import OUTPUT_FORMATS
from media_core.jobs import (JOB_ID_PATTERN, JobStore, mark_expiration, new_id, new_token,
                             remove_storage_directory, safe_directory, sweep_expired_storage)
from media_core.models import MediaInfo, ProcessRequest, VideoOption
from media_core.names import safe_stem
from media_core.probe import inspect_file
from media_core.settings import Settings
from media_core.sources import DirectURLSource, LocalFileSource
from media_core.storage import sha256_file


log = logging.getLogger(__name__)
CONTENT_TYPES = {
    "mp4": "video/mp4", "m4a": "audio/mp4", "mp3": "audio/mpeg",
    "flac": "audio/flac", "ogg": "audio/ogg", "opus": "audio/ogg", "wav": "audio/wav",
}


class JobRequest(BaseModel):
    source_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_token: str = Field(min_length=1)
    output_type: Literal["video", "audio"]
    output_format: Literal["mp4", "m4a", "mp3", "original"]
    resolution: int | None = None
    audio_bitrate_kbps: int | None = None


class InspectResponse(BaseModel):
    source_id: str
    source_token: str
    expires_at: str
    media: MediaInfo
    video_options: list[VideoOption]
    audio_options: list[str]


class JobAcceptedResponse(BaseModel):
    job_id: str
    access_token: str
    status: Literal["queued"]
    status_url: str


class JobOutputResponse(BaseModel):
    format: str
    size_bytes: int
    media: MediaInfo


class JobErrorResponse(BaseModel):
    code: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    stage: str
    progress: int | None
    attempt: int
    last_attempt_progress: int | None = None
    created_at: str
    updated_at: str
    expires_at: str
    output_format: str
    resolution: int | None = None
    audio_bitrate_kbps: int | None = None
    output: JobOutputResponse | None = None
    download_url: str | None = None
    error: JobErrorResponse | None = None


def _media_error(error: MediaError) -> HTTPException:
    status = {
        ErrorCode.SIZE_LIMIT_EXCEEDED: 413,
        ErrorCode.UNAUTHORIZED_SOURCE: 403,
        ErrorCode.PROCESSING_TIMEOUT: 504,
        ErrorCode.INTERNAL_ERROR: 503,
        ErrorCode.INSUFFICIENT_DISK_SPACE: 503,
    }.get(error.code, 400)
    message = "Media operation failed" if status == 503 else str(error)
    return HTTPException(status_code=status, detail={"code": error.code.value, "message": message})


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer job capability required")
    return authorization[7:]


def _public_job(record: dict) -> dict:
    selection = record["request"]
    result = {
        "job_id": record["id"], "status": record["status"], "stage": record["stage"],
        "progress": record["progress"], "attempt": record["attempts"],
        "last_attempt_progress": record.get("last_attempt_progress"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"], "expires_at": record["expires_at"],
        "output_format": selection["output_format"], "resolution": selection.get("resolution"),
        "audio_bitrate_kbps": selection.get("audio_bitrate_kbps"),
    }
    if record["status"] == "completed" and record["result"]:
        output = record["result"]
        result["output"] = {
            "format": output["output_format"], "size_bytes": output["file_size"],
            "media": output["media"],
        }
        result["download_url"] = f"/api/v1/jobs/{record['id']}/download"
    if record["status"] in ("failed", "cancelled"):
        result["error"] = record["error"]
    return result


def _open_published_output(root: Path, job_id: str, result: dict) -> tuple[BinaryIO, str, str, int]:
    """Bind a completed download to a regular file before cleanup can unlink it."""
    try:
        filename = result["filename"]
        output_format = result["output_format"]
        expected_size = result["file_size"]
        checksum = result.get("sha256")
        media = MediaInfo.model_validate(result["media"])
        if (not JOB_ID_PATTERN.fullmatch(job_id) or
                not isinstance(filename, str) or len(filename) > 160 or
                not isinstance(output_format, str) or output_format not in CONTENT_TYPES or
                filename != f"{safe_stem(filename)}.{output_format}" or
                not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size <= 0 or
                media.file_size != expected_size or
                media.container != OUTPUT_FORMATS[output_format].container or
                checksum is not None and (not isinstance(checksum, str) or len(checksum) != 64 or
                                          any(c not in "0123456789abcdef" for c in checksum))):
            raise ValueError("Invalid published result")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        directories = []
        file_descriptor = None
        try:
            current = os.open(root, directory_flags)
            directories.append(current)
            for segment in ("jobs", job_id, "output"):
                current = os.open(segment, directory_flags, dir_fd=current)
                directories.append(current)
            file_descriptor = os.open(filename, file_flags, dir_fd=current)
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
                raise ValueError("Published output is unavailable")
            opened = os.fdopen(file_descriptor, "rb")
            file_descriptor = None
            if checksum is not None:
                try:
                    if sha256_file(opened) != checksum:
                        raise ValueError("Published output checksum does not match")
                    opened.seek(0)
                except BaseException:
                    opened.close()
                    raise
            return opened, filename, output_format, expected_size
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            for descriptor in reversed(directories):
                os.close(descriptor)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise HTTPException(status_code=410, detail="Completed file is unavailable") from error


def create_app(*, redis_client: Redis | None = None, settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Media Downloader API", version="0.2.0")
    config = settings or Settings()
    connection = redis_client if redis_client is not None else Redis.from_url(config.redis_url)
    store = JobStore(connection, config)
    queue = Queue("media", connection=connection)
    root = config.media_storage_dir.resolve()
    last_sweep = 0.0

    def recover_expired_storage() -> None:
        nonlocal last_sweep
        now = time.time()
        if now - last_sweep < 60:
            return
        try:
            sweep_expired_storage(root, connection, now=now, settings=config)
        except Exception:
            log.exception("Expired media recovery failed")
            return
        last_sweep = now

    @app.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/media/inspect", status_code=201, response_model=InspectResponse, tags=["media"])
    async def inspect_media(request: Request):
        recover_expired_storage()
        source_id, source_token = new_id(), new_token()
        source_dir = safe_directory(root, "sources", source_id)
        created = False
        accepted = False
        try:
            source_dir.mkdir(parents=True, exist_ok=False)
            content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type == "application/json":
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 16 * 1024:
                        raise HTTPException(status_code=413, detail="URL request is too large")
                try:
                    url = json.loads(body)["url"]
                except (ValueError, TypeError, KeyError):
                    raise HTTPException(status_code=400, detail="A direct media URL is required") from None
                if not isinstance(url, str):
                    raise HTTPException(status_code=400, detail="A direct media URL is required")
                path = DirectURLSource(config.allowed_hosts, max_bytes=config.max_input_bytes).stage(url, source_dir).path
            elif content_type == "application/octet-stream":
                length = request.headers.get("content-length")
                if length and length.isdecimal() and int(length) > config.max_input_bytes:
                    raise HTTPException(status_code=413, detail="Upload exceeds the input size limit")
                supplied = request.headers.get("x-filename", "media.bin")
                suffix = Path(supplied.replace("\\", "/").rsplit("/", 1)[-1]).suffix.lower()
                if len(suffix) > 11 or not suffix[1:].isalnum():
                    suffix = ".bin"
                path = source_dir / (safe_stem(supplied) + suffix)
                incoming = source_dir / ".incoming"
                size = 0
                with incoming.open("xb") as output:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > config.max_input_bytes:
                            raise HTTPException(status_code=413, detail="Upload exceeds the input size limit")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if not size:
                    raise HTTPException(status_code=400, detail="Upload is empty")
                os.replace(incoming, path)
                LocalFileSource(source_dir, max_bytes=config.max_input_bytes).validate(path.name)
            else:
                raise HTTPException(status_code=415, detail="Send application/json with url or application/octet-stream")
            media = inspect_file(path)
            record = store.create_source(source_id, source_token, path.name, media.model_dump())
            created = True
            mark_expiration(source_dir, record["expires_epoch"])
            scheduled = queue.enqueue_in(
                timedelta(seconds=config.source_ttl_seconds), "media_worker.jobs.cleanup_source", source_id,
            )
            if scheduled is None:
                raise RuntimeError("Source cleanup was not scheduled")
            accepted = True
            return {
                "source_id": source_id, "source_token": source_token,
                "expires_at": record["expires_at"],
                "media": media.model_dump(),
                "video_options": [option.model_dump() for option in video_options(media)],
                "audio_options": ["m4a", "mp3", "original"] if media.audio_streams else [],
            }
        except (HTTPException, MediaError) as error:
            shutil.rmtree(source_dir, ignore_errors=True)
            if isinstance(error, MediaError):
                raise _media_error(error) from error
            raise
        except Exception:
            shutil.rmtree(source_dir, ignore_errors=True)
            log.exception("Media inspection failed")
            raise HTTPException(status_code=503, detail="Media inspection is temporarily unavailable") from None
        finally:
            if created and not accepted:
                try:
                    connection.delete(store._source_key(source_id))
                except Exception:
                    log.exception("Could not roll back source record", extra={"source_id": source_id})

    @app.post("/api/v1/jobs", status_code=202, response_model=JobAcceptedResponse, tags=["jobs"])
    def create_job(body: JobRequest):
        recover_expired_storage()
        try:
            source = store.source(body.source_id, body.source_token)
        except Exception:
            log.exception("Source lookup failed")
            raise HTTPException(status_code=503, detail="Job submission is temporarily unavailable") from None
        if not source:
            raise HTTPException(status_code=403, detail="Source capability is invalid or expired")
        source_dir = safe_directory(root, "sources", body.source_id)
        original = source_dir / source["filename"]
        if not original.is_file() or not original.resolve().is_relative_to(source_dir):
            raise HTTPException(status_code=410, detail="Source media has expired")
        job_id, access_token = new_id(), new_token()
        job_dir = safe_directory(root, "jobs", job_id)
        request_data = body.model_dump(exclude={"source_id", "source_token"})
        processing = ProcessRequest(source_reference=source["filename"], output_dir=job_dir / "output", **request_data)
        engine = MediaEngine(EngineConfig(max_output_bytes=config.max_output_bytes))
        try:
            engine._validate(processing)
            engine._plan(original, MediaInfo.model_validate(source["media"]), processing)
        except MediaError as error:
            raise _media_error(error) from error
        except Exception:
            log.exception("Job validation failed")
            raise HTTPException(status_code=503, detail="Job submission is temporarily unavailable") from None
        created = False
        directory_created = False
        enqueue_attempted = False
        try:
            job_dir.mkdir(parents=True, exist_ok=False)
            directory_created = True
            LocalFileSource(source_dir, max_bytes=config.max_input_bytes).stage(
                source["filename"], job_dir / "input",
            )
            with connection.lock("media:queue:admission", timeout=15, blocking_timeout=3):
                if len(queue) >= config.queue_capacity:
                    raise HTTPException(status_code=429, detail="Media queue is full")
                record = store.create_job(job_id, access_token, body.source_id, source["filename"], request_data)
                created = True
                mark_expiration(job_dir, record["expires_epoch"])
                scheduled = queue.enqueue_in(
                    timedelta(seconds=max(1, int(record["expires_epoch"] - time.time()) + 1)),
                    "media_worker.jobs.cleanup_job", job_id,
                )
                if scheduled is None:
                    raise RuntimeError("Job cleanup was not scheduled")
                enqueue_attempted = True
                enqueued = queue.enqueue(
                    "media_worker.jobs.process_media_job", job_id, job_id=job_id,
                    job_timeout=config.job_timeout_seconds, ttl=config.queue_wait_seconds,
                    retry=Retry(max=config.max_job_attempts - 1, interval=config.retry_delay_seconds)
                    if config.max_job_attempts > 1 else None,
                    result_ttl=config.job_retention_seconds, failure_ttl=config.job_retention_seconds,
                )
                if enqueued is None:
                    raise RuntimeError("RQ did not return an enqueued job")
        except MediaError as error:
            if directory_created:
                shutil.rmtree(job_dir, ignore_errors=True)
            raise _media_error(error) from error
        except HTTPException:
            if directory_created:
                shutil.rmtree(job_dir, ignore_errors=True)
            raise
        except Exception:
            if enqueue_attempted:
                try:
                    store.cancel(job_id)
                    Job.fetch(job_id, connection=connection).cancel()
                except Exception:
                    log.exception("Could not cancel ambiguous enqueue", extra={"job_id": job_id})
            else:
                if directory_created:
                    shutil.rmtree(job_dir, ignore_errors=True)
                if created:
                    try:
                        connection.delete(store._job_key(job_id))
                    except Exception:
                        log.exception("Could not roll back job record", extra={"job_id": job_id})
            log.exception("Job submission failed", extra={"job_id": job_id})
            raise HTTPException(status_code=503, detail="Job submission is temporarily unavailable") from None
        return {"job_id": job_id, "access_token": access_token, "status": "queued",
                "status_url": f"/api/v1/jobs/{job_id}"}

    def authorized_job(job_id: str, authorization: str | None) -> dict:
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise HTTPException(status_code=404, detail="Job was not found or has expired")
        token = _bearer(authorization)
        try:
            record = store.authorized(job_id, token)
        except Exception:
            log.exception("Job lookup failed", extra={"job_id": job_id})
            raise HTTPException(status_code=503, detail="Job status is temporarily unavailable") from None
        if not record:
            raise HTTPException(status_code=404, detail="Job was not found or has expired")
        if record["expires_epoch"] <= time.time():
            raise HTTPException(status_code=410, detail="Job has expired")
        return record

    @app.get("/api/v1/jobs/{job_id}", response_model=JobStatusResponse,
             response_model_exclude_none=True, tags=["jobs"])
    def get_job(job_id: str, authorization: str | None = Header(default=None)):
        return _public_job(authorized_job(job_id, authorization))

    @app.post("/api/v1/jobs/{job_id}/cancel", tags=["jobs"])
    def cancel_job(job_id: str, authorization: str | None = Header(default=None)):
        record = authorized_job(job_id, authorization)
        # Completion wins if it reached Redis before this cancellation request.
        if record["status"] in ("completed", "failed", "cancelled"):
            return _public_job(record)
        cancelled = store.cancel(job_id)
        if not cancelled:
            return _public_job(store.authorized(job_id, _bearer(authorization)))
        if cancelled["status"] == "cancelled" and cancelled.get("claim_id") is None:
            try:
                Job.fetch(job_id, connection=connection).cancel()
            except Exception:
                log.exception("Could not remove queued RQ job", extra={"job_id": job_id})
            try:
                remove_storage_directory(safe_directory(root, "jobs", job_id), cancelled["expires_epoch"])
            except OSError:
                log.exception("Cancelled job storage cleanup deferred", extra={"job_id": job_id})
        return _public_job(cancelled)

    @app.get("/api/v1/jobs/{job_id}/download", tags=["jobs"])
    def download(job_id: str, authorization: str | None = Header(default=None)):
        record = authorized_job(job_id, authorization)
        if record["status"] != "completed" or not record["result"]:
            raise HTTPException(status_code=409, detail="Job is not completed")
        opened, filename, output_format, size = _open_published_output(root, job_id, record["result"])

        def stream():
            try:
                while chunk := opened.read(64 * 1024):
                    yield chunk
            finally:
                opened.close()

        return StreamingResponse(
            stream(), media_type=CONTENT_TYPES[output_format],
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                "Content-Length": str(size), "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
            background=BackgroundTask(opened.close),
        )

    return app
