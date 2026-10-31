# Media Downloader

A local development scaffold for a media processing application. The project accepts only media that users own or are authorized to download. The API accepts media, queues conversion jobs, reports status, and serves completed output to holders of each job's capability token. The worker uses the Phase 2 media engine.

## Run with Docker Compose

Requirements: Docker with Compose. From the repository root:

```sh
docker compose up --build
```

Open <http://localhost:3000>. The API health endpoint is <http://localhost:8000/api/health>; it returns `{"status":"ok"}`. Stop with `docker compose down`. To remove local Redis and media volumes too, run `docker compose down -v`.

The Compose stack runs four services: `web`, `api`, `worker`, and `redis`. The API and worker share a Docker volume at `/data`. Redis is private to the Compose network. The web and API ports bind to localhost.

## Run services on the host

Node.js 22, Python 3.12 or newer, Redis, FFmpeg, and ffprobe are needed for host-based development. Copy `.env.example` to `.env` and export the values in your shell; the services do not automatically read that file.

```sh
npm ci
npm run dev:web
```

In another shell, create a virtual environment and install the Python packages:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ./packages/media_core -e './services/api[test]' -e ./services/worker
uvicorn media_api.main:app --reload
```

On this Mac, the working project-local Python 3.13.18 interpreter is `.python-runtime/python/bin/python3.13`; use it in place of `python3.12` when recreating `.venv`. The project-local runtime is ignored by Git.

To start the worker after Redis is running:

```sh
source .venv/bin/activate
python -m media_worker.main
```

Set `API_INTERNAL_URL=http://localhost:8000` when running the web app outside Compose.

## Checks

```sh
npm run lint:web
npm run build:web
source .venv/bin/activate
pytest packages/media_core/tests services/api/tests services/worker/tests
docker compose config
```

## Media engine

The engine is called from Python independently of the API and RQ worker. It accepts staged local uploads beneath an explicitly chosen root, or direct HTTPS URLs from exact allowlisted hostnames. It inspects streams with ffprobe, offers feasible MP4 resolutions, and produces MP4, M4A, MP3, or compatible original-audio files. See [the media engine guide](docs/media_engine.md) for exact local test commands, configuration, and limits.

## Configuration

| Variable | Used by | Default | Purpose |
| --- | --- | --- | --- |
| `API_INTERNAL_URL` | web server | `http://localhost:8000` | Server-side target for the `/api/*` rewrite. Compose uses `http://api:8000`. |
| `REDIS_URL` | API and worker | `redis://localhost:6379/0` | Queue connection. Compose uses its private Redis service. |
| `MEDIA_STORAGE_DIR` | API and worker | `./data` | Shared temporary media directory. Compose uses `/data`. |
| `JOB_RETENTION_SECONDS` | API and worker | `86400` | Time to retain a terminal job and its output. |

`API_INTERNAL_URL` is a trusted deployment setting. Do not set it from a user-supplied URL.

## Operational limits

This is a development scaffold, not a public download service. The Phase 3 API, worker, delivery, and cleanup paths have been verified with real Docker Compose, Redis, RQ, and FFmpeg using local fixtures. Expired storage is recovered by scheduled cleanup, worker maintenance, or a later API inspection/job submission. If worker activity and API inspection/job submission all stop, expired files remain until one resumes. Directories with neither an expiry marker nor a deletion marker are preserved for manual review. Cancellation during a bounded source copy takes effect after that copy finishes. Public deployment still needs rate limiting, observability, and production configuration. The Compose file uses development commands and bind mounts.

See [the Phase 3 implementation and verification report](docs/PHASE_3_IMPLEMENTATION.md) for endpoints, lifecycle, settings, and test results.

See [the Phase 4 typed API client guide](docs/PHASE_4_MILESTONE_A.md) for the browser contract, credential handling, and frontend verification commands.
See [the Phase 4 submission and progress guide](docs/PHASE_4_MILESTONE_B.md) for the browser workflow and polling behavior.
See [the Phase 4 secure download guide](docs/PHASE_4_MILESTONE_C.md) for result delivery and browser memory limits.

See [the architecture document](docs/architecture.md) for data flow, boundaries, and planned phases.

