# Phase 4 Milestone A: typed browser API integration

The Next.js application still has its scaffold page. This milestone adds a
browser-ready client for the Phase 3 backend without adding the downloader UI.
The implementation is in `apps/web/src/lib/api/`:

| Module | Responsibility |
| --- | --- |
| `types.ts` | FastAPI wire types, job states, capabilities, and output types |
| `validation.ts` | Runtime checks for successful JSON responses |
| `errors.ts` | Safe, categorized API errors |
| `client.ts` | Same-origin fetch, base path, timeout, abort, and HTTP handling |
| `media.ts` | Typed endpoint functions and browser download helper |

## Backend contract

| Operation | Request | Response | Authorization |
| --- | --- | --- | --- |
| Inspect URL | `POST /api/v1/media/inspect`, JSON `{ "url": "https://..." }` | `201` with source ID, source token, expiry, media, video/audio options | None |
| Inspect upload | Same endpoint, raw `application/octet-stream` body and optional `X-Filename` | Same `201` response | None |
| Create job | `POST /api/v1/jobs`, JSON with source ID/token and output selection | `202` with job ID, access token, queued status, status URL | Source capability in body |
| Status | `GET /api/v1/jobs/{id}` | Job state, stage, progress, attempt, expiry; output or error when terminal | `Authorization: Bearer <job token>` |
| Cancel | `POST /api/v1/jobs/{id}/cancel` | Current job status; queued cancellation can be terminal immediately | Same bearer header |
| Download | `GET /api/v1/jobs/{id}/download` | Media bytes with content type, length, and attachment filename | Same bearer header |

The backend does **not** accept multipart form uploads. The client sends the
file as a raw octet stream. Job submission is JSON. The backend may return
an HTTP error with a string `detail`, a `{code, message}` detail, or FastAPI
validation details. The client normalizes 400, 401, 403, 404, 409, 410, 413,
415, 422, 429, 500, 503, and 504. Network failure, user abort, timeout,
malformed success JSON, and missing capability have separate error kinds.
Server stack traces, storage paths, and echoed capability tokens are not used
as public error messages.

`GET` status responses omit optional `null` fields because FastAPI uses
`response_model_exclude_none`. In particular, missing `progress` is parsed
as `null` for an unknown duration or retry. `last_attempt_progress` is
optional. A completed status includes output metadata and a relative
`download_url`; the client validates this field but constructs its own
download path from the job ID. It never puts a token in a URL.

## Browser use and credentials

```ts
import { MediaApi, saveDownloadedResult } from "@/lib/api/media.ts";

const api = new MediaApi();
const source = await api.inspectUpload(file, file.name, { signal });
const job = await api.createJob(source, {
  output_type: "audio", output_format: "mp3", audio_bitrate_kbps: 128,
});
const status = await api.getJobStatus(job);
if (status.status === "completed") {
  saveDownloadedResult(await api.downloadJob(job));
}
```

The `source_token` and `access_token` are returned by FastAPI. Keep the source
and accepted job objects in browser memory, such as client component state.
The status, cancel, and download methods accept a `JobCapability` object
containing the matching `job_id` and `access_token`. The client has no token
store, cookie, local storage, session storage, logging, or analytics hook.
After a page refresh, those capabilities are lost and the user must inspect
and submit again. Account-backed recovery is outside this milestone.

`downloadJob` fetches with the bearer header, validates media type and size,
and sanitizes the attachment filename. `saveDownloadedResult` creates a
temporary browser object URL after a user action. It does not use the API's
`download_url` as a token-bearing browser navigation. The Blob approach holds
the downloaded output in browser memory; very large outputs may need a later
streaming or file-system-specific experience.

## Configuration

The browser uses `/api` on the same origin. `apps/web/next.config.ts` proxies
that prefix to FastAPI. `API_INTERNAL_URL` is a **server-only** Next.js setting:
it defaults to `http://localhost:8000` for host development and Compose sets
`http://api:8000`. No `NEXT_PUBLIC_*` backend URL, Redis setting, storage path,
or new `.env` file is needed. This design avoids browser CORS configuration.

## Verification

From the repository root:

```sh
npm run typecheck --workspace @media-downloader/web
npm run lint --workspace @media-downloader/web
npm run test --workspace @media-downloader/web
npm run build:web
git diff --check
```

The frontend tests use Node 22's built-in test runner and mocked fetch. They
need no additional package. Browser-to-backend verification through the
Next.js rewrite is reserved for Phase 4 Milestone E. Docker, Redis, and the
backend test suite are not required for this milestone.
