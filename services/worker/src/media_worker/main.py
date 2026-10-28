import logging
import time
import uuid
from datetime import datetime

from redis import Redis
from rq import Queue, Retry, Worker

from media_core.jobs import JOB_ID_PATTERN, JobStore, sweep_expired_storage
from media_core.settings import Settings


log = logging.getLogger(__name__)


def recover_abandoned_jobs(settings: Settings, connection: Redis) -> None:
    """Requeue claims left by a dead worker after its execution lease expires."""
    root = settings.media_storage_dir.resolve() / "jobs"
    if not root.is_dir():
        return
    store = JobStore(connection, settings)
    queue = Queue("media", connection=connection)
    now = time.time()

    def enqueue_recovery(job_id: str) -> None:
        queue.enqueue(
            "media_worker.jobs.process_media_job", job_id, job_id=f"recovery-{uuid.uuid4().hex}",
            job_timeout=settings.job_timeout_seconds, ttl=settings.queue_wait_seconds,
            retry=Retry(max=settings.max_job_attempts - 1, interval=settings.retry_delay_seconds)
            if settings.max_job_attempts > 1 else None,
            failure_ttl=settings.job_retention_seconds,
        )

    for directory in root.iterdir():
        if not directory.is_dir() or not JOB_ID_PATTERN.fullmatch(directory.name):
            continue
        job_id = directory.name
        record = store.get(job_id)
        if not record:
            continue
        if record["status"] == "processing":
            deadline = record.get("claim_deadline")
            if deadline is None:
                deadline = datetime.fromisoformat(record["updated_at"]).timestamp() + settings.job_timeout_seconds + 60
            if deadline > now:
                continue
            if record["cancel_requested"]:
                enqueue_recovery(job_id)
                continue
            claim_id = record.get("claim_id")
            if claim_id and JOB_ID_PATTERN.fullmatch(claim_id) and (
                directory / f".result-{claim_id}.json"
            ).is_file():
                enqueue_recovery(job_id)
                continue
            if record["attempts"] >= settings.max_job_attempts:
                store.transition_owned(
                    job_id, record.get("claim_id"), "failed", stage="failed",
                    error={"code": "processing_timeout", "message": "Media processing timed out"},
                )
                continue
            record = store.transition_owned(
                job_id, record.get("claim_id"), "queued", stage="recovering",
                claim_id=None, claim_deadline=None, recovery_pending=True,
            )
        if not record or record["status"] != "queued" or not record.get("recovery_pending"):
            continue
        enqueue_recovery(job_id)
        store.mutate(job_id, lambda current: {
            **current, "recovery_pending": False,
        } if current["status"] == "queued" else None)
        log.warning("Recovered abandoned media job", extra={"job_id": job_id})


class CleanupWorker(Worker):
    """Recover expired media during RQ's normal periodic maintenance."""

    def run_maintenance_tasks(self) -> None:
        super().run_maintenance_tasks()
        try:
            settings = Settings()
            recover_abandoned_jobs(settings, self.connection)
            sweep_expired_storage(settings.media_storage_dir.resolve(), self.connection, settings=settings)
        except Exception:
            log.exception("Expired media recovery failed")


def main() -> None:
    settings = Settings()
    connection = Redis.from_url(settings.redis_url)
    queue = Queue("media", connection=connection)
    CleanupWorker([queue], connection=connection).work(with_scheduler=True)


if __name__ == "__main__":
    main()

