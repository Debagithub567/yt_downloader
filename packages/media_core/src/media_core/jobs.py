"""Redis-backed source capabilities and guarded media-job state transitions."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from redis import Redis, WatchError

from .settings import Settings


TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return secrets.token_hex(16)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def safe_directory(root: Path, category: str, identifier: str) -> Path:
    if category not in {"jobs", "sources"} or not JOB_ID_PATTERN.fullmatch(identifier):
        raise ValueError("Invalid identifier")
    if (root / category).is_symlink():
        raise ValueError("Storage category is not a directory")
    return root / category / identifier


def mark_expiration(directory: Path, expires_epoch: float) -> None:
    """Leave a durable cleanup deadline independent of Redis and RQ scheduling."""
    marker = directory / ".expires"
    with marker.open("x", encoding="ascii") as output:
        output.write(str(expires_epoch))
        output.flush()
        os.fsync(output.fileno())


def update_expiration(directory: Path, expires_epoch: float) -> None:
    """Move the recovery deadline to the terminal job's retention deadline."""
    marker = directory / ".expires"
    with tempfile.NamedTemporaryFile("w", dir=directory, prefix=".expires-", delete=False,
                                     encoding="ascii") as output:
        temporary = Path(output.name)
        try:
            output.write(str(expires_epoch))
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, marker)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _deletion_marker(directory: Path) -> Path:
    return directory.parent / f".deleting-{directory.name}"


def _has_deletion_intent(directory: Path) -> bool:
    marker = _deletion_marker(directory)
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="ascii") == directory.name
    except (OSError, UnicodeError):
        return False


def _mark_deletion(directory: Path) -> None:
    marker = _deletion_marker(directory)
    with tempfile.NamedTemporaryFile("w", dir=directory.parent, prefix=".deleting-next-",
                                     delete=False, encoding="ascii") as output:
        temporary = Path(output.name)
        try:
            output.write(directory.name)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, marker)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def remove_storage_directory(directory: Path, expires_epoch: float) -> None:
    """Persist deletion intent outside the directory before removing any files."""
    if (directory.parent.name not in {"jobs", "sources"} or
            not JOB_ID_PATTERN.fullmatch(directory.name) or directory.parent.is_symlink()):
        raise ValueError("Invalid storage directory")
    marker = _deletion_marker(directory)
    if not directory.exists() and not directory.is_symlink():
        if _has_deletion_intent(directory):
            marker.unlink(missing_ok=True)
        return
    _mark_deletion(directory)
    if directory.is_symlink():
        directory.unlink()
    else:
        try:
            shutil.rmtree(directory)
        except OSError:
            if directory.is_dir() and not directory.is_symlink():
                try:
                    update_expiration(directory, expires_epoch)
                except OSError:
                    log.exception("Could not restore expiry marker after partial cleanup")
            raise
    marker.unlink(missing_ok=True)


def cleanup_expired_job(root: Path, redis: Redis, settings: Settings, job_id: str,
                        *, now: float | None = None) -> tuple[bool, float | None]:
    """Delete expired job storage under the same lock used by processing workers."""
    now = time.time() if now is None else now
    path = safe_directory(root, "jobs", job_id)
    lock = redis.lock(f"media:job:worker:{job_id}", timeout=30, blocking=False)
    if not lock.acquire(blocking=False):
        return False, 5
    try:
        key = f"media:job:{job_id}"
        raw = redis.get(key)
        record = json.loads(raw) if raw else None
        if record:
            if record["expires_epoch"] > now and not (
                record["status"] in {"failed", "cancelled"} and _has_deletion_intent(path)
            ):
                return False, max(1, record["expires_epoch"] - now)
            if record["status"] == "processing":
                deadline = record.get("claim_deadline")
                if deadline is None:
                    deadline = (datetime.fromisoformat(record["updated_at"]).timestamp()
                                + settings.job_timeout_seconds + 60)
                if deadline > now:
                    return False, max(1, deadline - now)
        remove_storage_directory(path, record["expires_epoch"] if record else now)
        redis.delete(key)
        return True, None
    finally:
        try:
            lock.release()
        except Exception:
            log.warning("Could not release cleanup lock", extra={"job_id": job_id})


def sweep_expired_storage(root: Path, redis: Redis, *, now: float | None = None,
                          settings: Settings | None = None) -> int:
    """Recover expired directories if their scheduled cleanup did not run."""
    now = time.time() if now is None else now
    settings = settings or Settings(media_storage_dir=root)
    removed = 0
    for category in ("sources", "jobs"):
        parent = root / category
        if not parent.is_dir() or parent.is_symlink():
            continue
        for marker in parent.glob(".deleting-*"):
            identifier = marker.name.removeprefix(".deleting-")
            if not JOB_ID_PATTERN.fullmatch(identifier):
                continue
            directory = safe_directory(root, category, identifier)
            if not _has_deletion_intent(directory):
                log.warning("Invalid deletion marker preserved", extra={"path": str(marker)})
                continue
            if category == "jobs":
                try:
                    deleted, _ = cleanup_expired_job(root, redis, settings, identifier, now=now)
                    removed += int(deleted)
                except OSError:
                    log.exception("Could not resume interrupted job cleanup", extra={"job_id": identifier})
                continue
            key = f"media:source:{identifier}"
            raw = redis.get(key)
            if raw and (redis.ttl(key) > 0 or json.loads(raw)["expires_epoch"] > now):
                continue
            try:
                remove_storage_directory(directory, now)
                redis.delete(key)
                removed += 1
            except OSError:
                log.exception("Could not resume interrupted source cleanup", extra={"source_id": identifier})
        for directory in parent.iterdir():
            if not directory.is_dir() or directory.is_symlink() or not JOB_ID_PATTERN.fullmatch(directory.name):
                continue
            marker = directory / ".expires"
            try:
                if float(marker.read_text(encoding="ascii")) > now:
                    continue
            except (OSError, ValueError):
                if not _has_deletion_intent(directory):
                    log.warning("Unmarked media directory preserved", extra={"path": str(directory)})
                continue
            if category == "jobs":
                try:
                    deleted, _ = cleanup_expired_job(root, redis, settings, directory.name, now=now)
                    removed += int(deleted)
                except OSError:
                    log.exception("Could not remove expired job storage", extra={"job_id": directory.name})
                continue
            key = f"media:source:{directory.name}"
            raw = redis.get(key)
            if raw and (redis.ttl(key) > 0 or json.loads(raw)["expires_epoch"] > now):
                continue
            try:
                remove_storage_directory(directory, now)
                redis.delete(key)
                removed += 1
            except OSError:
                log.exception("Could not remove expired source storage", extra={"source_id": directory.name})
    return removed


class JobStore:
    def __init__(self, redis: Redis, settings: Settings):
        self.redis = redis
        self.settings = settings

    @staticmethod
    def _source_key(source_id: str) -> str:
        return f"media:source:{source_id}"

    @staticmethod
    def _job_key(job_id: str) -> str:
        return f"media:job:{job_id}"

    def create_source(self, source_id: str, token: str, filename: str, media: dict) -> dict:
        expires = time.time() + self.settings.source_ttl_seconds
        record = {
            "id": source_id, "token_hash": token_hash(token), "filename": filename,
            "media": media, "created_at": utc_now(),
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
            "expires_epoch": expires,
        }
        if not self.redis.set(self._source_key(source_id), json.dumps(record), ex=self.settings.source_ttl_seconds, nx=True):
            raise RuntimeError("Source identifier collision")
        return record

    def source(self, source_id: str, token: str) -> dict | None:
        if not JOB_ID_PATTERN.fullmatch(source_id):
            return None
        raw = self.redis.get(self._source_key(source_id))
        if not raw:
            return None
        record = json.loads(raw)
        if record["expires_epoch"] <= time.time():
            return None
        if not hmac.compare_digest(record["token_hash"], token_hash(token)):
            return None
        return record

    def create_job(self, job_id: str, token: str, source_id: str, filename: str, request: dict) -> dict:
        now = time.time()
        expires = (
            now + self.settings.queue_wait_seconds
            + self.settings.max_job_attempts * self.settings.job_timeout_seconds
            + max(0, self.settings.max_job_attempts - 1) * self.settings.retry_delay_seconds
            + self.settings.job_retention_seconds
        )
        record = {
            "id": job_id, "token_hash": token_hash(token), "source_id": source_id,
            "source_filename": filename, "status": "queued", "stage": "queued", "progress": 0,
            "created_at": utc_now(), "updated_at": utc_now(),
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
            "expires_epoch": expires, "request": request, "attempts": 0,
            "cancel_requested": False, "error": None, "result": None,
            "claim_id": None, "claim_deadline": None, "recovery_pending": False,
            "last_attempt_progress": None,
        }
        if not self.redis.set(self._job_key(job_id), json.dumps(record), ex=max(1, int(expires - now)), nx=True):
            raise RuntimeError("Job identifier collision")
        return record

    def get(self, job_id: str) -> dict | None:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            return None
        raw = self.redis.get(self._job_key(job_id))
        return json.loads(raw) if raw else None

    def authorized(self, job_id: str, token: str) -> dict | None:
        record = self.get(job_id)
        if record and hmac.compare_digest(record["token_hash"], token_hash(token)):
            return record
        return None

    def mutate(self, job_id: str, change: Callable[[dict], dict | None]) -> dict | None:
        key = self._job_key(job_id)
        for _ in range(8):
            try:
                with self.redis.pipeline() as pipe:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if not raw:
                        return None
                    current = json.loads(raw)
                    updated = change(current.copy())
                    if updated is None:
                        return None
                    updated["updated_at"] = utc_now()
                    ttl = max(1, int(updated["expires_epoch"] - time.time()))
                    pipe.multi()
                    pipe.set(key, json.dumps(updated), ex=ttl)
                    pipe.execute()
                    return updated
            except WatchError:
                continue
        raise RuntimeError("Job state changed too frequently")

    def transition(self, job_id: str, allowed: set[str], target: str, **fields: Any) -> dict | None:
        def change(record: dict) -> dict | None:
            if record["status"] not in allowed:
                return None
            record.update(fields)
            record["status"] = target
            if target in TERMINAL_STATES:
                expiry = time.time() + self.settings.job_retention_seconds
                record["expires_epoch"] = expiry
                record["expires_at"] = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
            return record
        return self.mutate(job_id, change)

    def claim(self, job_id: str, claim_id: str) -> dict | None:
        """Claim a queued attempt; ownership is checked again at publication."""
        def change(record: dict) -> dict | None:
            if (record["status"] != "queued" or record["expires_epoch"] <= time.time() or
                    record["attempts"] >= self.settings.max_job_attempts):
                return None
            record.update(
                status="processing", stage="preparing", progress=0,
                attempts=record["attempts"] + 1,
                claim_id=claim_id, claim_deadline=time.time() + self.settings.job_timeout_seconds + 60,
                recovery_pending=False,
            )
            return record
        return self.mutate(job_id, change)

    def transition_owned(self, job_id: str, expected_claim_id: str | None, target: str,
                         **fields: Any) -> dict | None:
        """Prevent a stale worker from overwriting a newer or terminal state."""
        def change(record: dict) -> dict | None:
            if record["status"] != "processing" or record.get("claim_id") != expected_claim_id:
                return None
            if target != "cancelled" and record["cancel_requested"]:
                return None
            record.update(fields)
            record["status"] = target
            if target == "queued":
                record["last_attempt_progress"] = record["progress"]
                record["progress"] = None
            if target in TERMINAL_STATES:
                expiry = time.time() + self.settings.job_retention_seconds
                record["expires_epoch"] = expiry
                record["expires_at"] = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
            return record
        return self.mutate(job_id, change)

    def progress(self, job_id: str, percent: int | None, stage: str,
                 *, claim_id: str | None = None) -> dict | None:
        def change(record: dict) -> dict | None:
            if record["status"] != "processing" or claim_id is not None and record.get("claim_id") != claim_id:
                return None
            if record["cancel_requested"]:
                return None
            if percent is not None:
                previous = record["progress"] if record["progress"] is not None else 0
                record["progress"] = max(previous, min(99, max(0, int(percent))))
            elif stage == "processing":
                record["progress"] = None
            record["stage"] = stage
            return record
        return self.mutate(job_id, change)

    def cancel(self, job_id: str) -> dict | None:
        def change(record: dict) -> dict | None:
            if record["status"] == "queued":
                record.update(
                    status="cancelled", cancel_requested=True, stage="cancelled",
                    error={"code": "cancelled", "message": "Processing was cancelled"},
                )
                expiry = time.time() + self.settings.job_retention_seconds
                record["expires_epoch"] = expiry
                record["expires_at"] = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
                return record
            if record["status"] == "processing" and not record["cancel_requested"]:
                record.update(cancel_requested=True, stage="cancelling")
                return record
            return None
        return self.mutate(job_id, change)
