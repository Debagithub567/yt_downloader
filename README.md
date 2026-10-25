# Media Downloader

A local development scaffold for a media processing application. The project accepts only media that users own or are authorized to download. Phase 1 contains the service layout, configuration contract, source-adapter interface, and a health endpoint. Media uploads, URL inspection, conversion, and downloads are not implemented yet.

## Run with Docker Compose

Requirements: Docker with Compose. From the repository root:

```sh
docker compose up --build
```

Open <http://localhost:3000>. The API health endpoint is <http://localhost:8000/api/health>; it returns `{"status":"ok"}`. Stop with `docker compose down`. To remove local Redis and media volumes too, run `docker compose down -v`.

The Compose stack runs four services: `web`, `api`, `worker`, and `redis`. The API and worker share a Docker volume at `/data`. Redis is private to the Compose network. The web and API ports bind to localhost.

## Run services on the host

Node.js 22, Python 3.12, Redis, FFmpeg, and ffprobe are needed for host-based development. Copy `.env.example` to `.env` and export the values in your shell; the services do not automatically read that file.

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
pytest packages/media_core/tests services/api/tests
docker compose config
```

## Configuration

| Variable | Used by | Default | Purpose |
| --- | --- | --- | --- |
| `API_INTERNAL_URL` | web server | `http://localhost:8000` | Server-side target for the `/api/*` rewrite. Compose uses `http://api:8000`. |
| `REDIS_URL` | API and worker | `redis://localhost:6379/0` | Queue connection. Compose uses its private Redis service. |
| `MEDIA_STORAGE_DIR` | API and worker | `./data` | Shared temporary media directory. Compose uses `/data`. |

`API_INTERNAL_URL` is a trusted deployment setting. Do not set it from a user-supplied URL.

## Operational limits

This is a development scaffold, not a public download service. It intentionally has no media input, processing, or delivery endpoints. Before public deployment, later phases must add allowlisted HTTPS URL handling with network protections, upload and request limits, job ownership and rate limiting, file expiry, cleanup, secure delivery, observability, and a production deployment configuration. The Compose file uses development commands and bind mounts.

See [the architecture document](docs/architecture.md) for data flow, boundaries, and planned phases.

