# Phase 4 Milestone C: secure result downloads

The completed-job page now offers **Download result** after the backend reports
`completed` with output metadata. It shows the output format, file size, and
`expires_at` availability time from the status response. The status response
does not contain a filename; the page shows the sanitized filename only after
the authorized download response supplies `Content-Disposition`.

The existing `MediaApi.downloadJob` sends `Authorization: Bearer <access_token>`
to `/api/v1/jobs/{job_id}/download` through the same-origin Next.js rewrite.
The capability never appears in the URL. The workflow keeps it only in memory,
blocks duplicate downloads, aborts an active transfer when a new job starts or
the page unmounts, and ignores a response from an old job. The existing save
helper creates a temporary browser object URL and revokes it after the click.
The workflow retains only the filename, not the downloaded Blob.

Before reading the body, the download helper requires the backend's
`Content-Length` to match the size in the completed job status. It checks the
received Blob size again afterward. This detects inconsistent responses and
avoids buffering a result whose advertised size is wrong. The browser still
buffers the complete result in a Blob before saving it; this is **not a
streaming browser download**. The backend default output limit is 1 GiB, so a
large valid result may use substantial browser memory. A future streaming
path would need a browser file-writing API or a server-side delivery design
that preserves bearer authorization; a token-bearing URL is not an option.

The UI distinguishes backend-confirmed expiry (`410`), missing completed file
(`410` with the backend's file-unavailable detail), denied access (`401/403`),
and unavailable or ambiguous `404` responses. A `404` cannot prove whether a
job expired or its capability is invalid because the backend intentionally
hides job existence. Network and service outages leave the result retryable.
Failed and cancelled jobs have no download action. The user can start a new
job from any terminal or unavailable state. Failed and cancelled jobs can keep
their inspected source and output choice when the source capability is still
valid; otherwise the input remains available for reinspection.

Capabilities are not persisted. Refreshing the page loses access to active
and completed jobs; the initial page explains this and offers the source form
again. Live browser-to-backend download verification through Next.js is
deferred to Milestone E.
