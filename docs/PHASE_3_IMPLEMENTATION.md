# Phase 3 backend implementation and verification

Phase 3 is the local API and worker workflow for authorized media conversion. It
uses FastAPI, Redis, RQ, FFmpeg, and a shared `/data` volume. The web application
is present in Compose, but its downloader interface is outside this phase.

## API

| Endpoint | Purpose | Authorization |
| --- | --- | --- |
| `GET /api/health` | Service health | None |
| `POST /api/v1/media/inspect` | Inspect an octet-stream upload, or a direct HTTPS URL on the configured host allowlist | Returns a source capability |
| `POST /api/v1/jobs` | Validate options and queue a conversion using `source_id` and `source_token` | Source capability in JSON body |
| `GET /api/v1/jobs/{id}` | Read state, attempt, progress, result, and expiry | `Authorization: Bearer <job token>` |
| `POST /api/v1/jobs/{id}/cancel` | Cancel a queued job or request cancellation of a running job | Job bearer token |
| `GET /api/v1/jobs/{id}/download` | Stream a completed output | Job bearer token |

An upload uses `Content-Type: application/octet-stream`; the optional
`X-Filename` header is sanitized. A direct URL uses JSON `{"url": "https://..."}`
and requires `MEDIA_ALLOWED_HOSTS`. Job submission selects `output_type`,
`output_format`, and any required resolution or bitrate. MP3 output requires a
supported `audio_bitrate_kbps` value. The API returns the job token once, at
submission. It stores only a SHA-256 hash of each capability token in Redis and
compares hashes with `hmac.compare_digest`. Job and source IDs are random 32-digit
hexadecimal values.

## Job lifecycle and Redis/RQ

The API stages authorized input in `/data/jobs/{id}/input`, creates a Redis
record, schedules a worst-case cleanup task, and enqueues
`media_worker.jobs.process_media_job` on the `media` queue. Both services use
the same Redis URL and storage volume. The RQ worker runs with its scheduler.

The worker acquires `media:job:worker:{id}`, claims the queued record with a
compare-and-set transition, and calls the Phase 2 `MediaEngine`. Its states are
`queued`, `processing`, and one of `completed`, `failed`, or `cancelled`.
Retries are bounded by `MAX_JOB_ATTEMPTS`; a new attempt has its own claim ID.
An abandoned claim is requeued by worker maintenance after its claim deadline.
Duplicate deliveries cannot replace an existing terminal result.

FFmpeg reports progress through `-progress pipe:1`. The worker reports stages
such as `preparing`, `processing`, and `finalizing`. Numeric progress remains
below 100 until completion. When duration is unknown, progress can be `null`.
Each status includes the attempt number and, after a retry, the previous
attempt's progress.

Queued cancellation first wins the Redis transition, then cancels the RQ job
and removes only that job's storage. Running cancellation sets
`cancel_requested`; the worker checks it around staging and inspection and
during FFmpeg execution, terminates the subprocess group, then removes its own
temporary files. A request during a bounded source copy can wait for that copy
to finish. A completed job remains completed if completion wins the race.

## Published output and authorization

The worker publishes output atomically, verifies its size and metadata, writes
a result manifest with the output filename and SHA-256 checksum, then commits
`completed` in Redis. The API downloads only a completed, unexpired job whose
bearer capability matches. It validates the stored filename, format, size,
media metadata, and checksum. Directory-relative file opens with `O_NOFOLLOW`
prevent path traversal and symlink escape. Opening the file before streaming
keeps an active download readable if cleanup unlinks the directory. Responses
use a sanitized UTF-8 attachment filename, an appropriate media content type,
`nosniff`, and `private, no-store`. Errors do not expose storage paths.

## Expiration and cleanup

Source capabilities default to one hour. A terminal job's Redis record and
output default to 24 hours; `JOB_RETENTION_SECONDS` controls this interval.
The initial job deadline includes queue wait, allowed attempts, retry delays,
and retention so cleanup cannot interrupt a valid queued or processing job.
The API schedules this task before processing enqueue. On completion, the
worker schedules another cleanup at the shorter terminal deadline. The initial
task remains a fallback if that scheduling fails. Redis record TTLs expire
independently of RQ.

Each managed directory has an `.expires` marker. Before removal, cleanup writes
a sibling `.deleting-{id}` marker so a crash after `.expires` disappears remains
recoverable. Cleanup obtains the same Redis lock as processing, checks the
claim deadline, and limits deletion to validated job/source directories.
Duplicate cleanup is safe. A failed filesystem or Redis step can be retried.
Worker maintenance and later API inspection/job submission sweep missed
cleanup. A directory with neither trustworthy marker is preserved for manual
review. If worker maintenance and API inspection/job submission all stop,
expired files wait until one resumes. A successfully completed job can be inaccessible after
expiry before its scheduled filesystem deletion runs.

## Configuration and local startup

| Variable | Default | Purpose |
| --- | --- | --- |
| `REDIS_URL` | `redis://localhost:6379/0` | Shared Redis database; Compose uses `redis://redis:6379/0` |
| `MEDIA_STORAGE_DIR` | `./data` | Shared root; Compose uses `/data` |
| `MEDIA_ALLOWED_HOSTS` | Empty | Allowlisted direct HTTPS hosts |
| `MAX_INPUT_BYTES` | `536870912` | Upload and source size limit |
| `MAX_OUTPUT_BYTES` | `1073741824` | Output size limit |
| `SOURCE_TTL_SECONDS` | `3600` | Source capability lifetime |
| `JOB_RETENTION_SECONDS` | `86400` | Terminal job retention |
| `QUEUE_WAIT_SECONDS` | `3600` | Maximum queue wait |
| `QUEUE_CAPACITY` | `100` | Submission admission limit |
| `JOB_TIMEOUT_SECONDS` | `360` | Worker job timeout |
| `MAX_JOB_ATTEMPTS` | `2` | Maximum processing attempts |
| `RETRY_DELAY_SECONDS` | `5` | Delay between attempts |

From the repository root:

```sh
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose logs --tail 50 api worker redis
```

If required packages or Dockerfile content changed since the images were
built, build only the affected services before `up -d`. The development
Compose file bind-mounts API, worker, and core source, and uses a shared named
storage volume. It is not a production deployment configuration.

## Verification performed on 2026-10-30

- Docker Engine 29.7.2 and Compose configuration were available. Existing
  images and the main four containers were reused. Redis answered `PONG`;
  the RQ worker listened on `media`. The API and web service started. The
  worker image had FFmpeg and ffprobe 7.1.5 on Linux `aarch64`.
- `docker compose exec -T api python /app/services/api/tests/live_phase3_integration.py`
  passed against real HTTP, Redis, RQ, FFmpeg, and shared storage. It observed
  queued and processing states, live video progress at 45% and 93%, completion at
  100%, an MP3 download with matching size and SHA-256, safe response headers,
  and successful ffprobe inspection. Missing/invalid tokens and an unknown ID
  were denied. Queued and running cancellation, a failed worker job, invalid
  input, and duplicate RQ delivery were checked. A running cancellation
  occurred after observed FFmpeg progress at 45%, ended cancelled, and left no output
  or lingering media subprocess.
- Short retention used disposable API and worker containers, Redis DB 15
  (verified empty first), and a unique `/data/milestone-e-*` directory. The
  containers set `JOB_RETENTION_SECONDS=3` without changing ordinary jobs.
  `live_phase3_retention.py` observed scheduled cleanup, expired download
  denial, Redis record and output removal, duplicate cleanup, worker lock and
  processing-claim protection, missed expiry recovery, deletion-marker recovery,
  markerless preservation, and a retry after an injected Redis delete failure.
  The RQ worker and scheduler logs confirmed the cleanup task ran.
- Focused host tests: 72 worker tests and 45 API/progress tests passed. The
  complete host suite passed: **156 passed, 0 failed, 0 skipped**, including
  the two loopback TLS tests that had previously been sandbox blocked. One
  existing Starlette deprecation warning remains. The container images do not
  include pytest; the opt-in scripts above supplied Linux container checks.

To reproduce the isolated retention check, first verify that Redis DB 15 is
empty. Choose another unused database if it is not. From the repository root:

```sh
docker compose exec -T api python -c 'from redis import Redis; assert Redis.from_url("redis://redis:6379/15").dbsize() == 0'
PHASE3_E_ID="$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
PHASE3_E_ROOT="/data/milestone-e-${PHASE3_E_ID}"
docker compose run -d --no-deps --name "milestone-e-api-${PHASE3_E_ID}" \
  -e REDIS_URL=redis://redis:6379/15 -e MEDIA_STORAGE_DIR="${PHASE3_E_ROOT}" \
  -e JOB_RETENTION_SECONDS=3 -e SOURCE_TTL_SECONDS=3 \
  -e MAX_JOB_ATTEMPTS=1 -e QUEUE_WAIT_SECONDS=15 -e JOB_TIMEOUT_SECONDS=30 \
  api uvicorn media_api.main:app --host 127.0.0.1 --port 8000
docker compose run -d --no-deps --name "milestone-e-worker-${PHASE3_E_ID}" \
  -e REDIS_URL=redis://redis:6379/15 -e MEDIA_STORAGE_DIR="${PHASE3_E_ROOT}" \
  -e JOB_RETENTION_SECONDS=3 -e SOURCE_TTL_SECONDS=3 \
  -e MAX_JOB_ATTEMPTS=1 -e QUEUE_WAIT_SECONDS=15 -e JOB_TIMEOUT_SECONDS=30 worker
docker exec "milestone-e-api-${PHASE3_E_ID}" python /app/services/api/tests/live_phase3_retention.py
docker rm -f "milestone-e-api-${PHASE3_E_ID}" "milestone-e-worker-${PHASE3_E_ID}"
```

The scripts create random job IDs and write only under the unique test root.
The two test containers can be removed without deleting any Docker volume.
Inspect and remove the empty test root and DB 15 test metadata separately.
Never use `docker compose down -v` for this check.

## Known limits

Direct observation of unknown-duration progress, a real Redis outage, a
worker crash during FFmpeg, and a forced FFmpeg process failure remain for
dedicated fault-injection testing. Unit tests cover their application logic,
but this milestone did not disrupt the shared services to simulate them.
There is no frontend workflow, object storage, rate limiting, production
observability, or production deployment configuration in Phase 3.
