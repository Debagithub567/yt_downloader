# Phase 5A.1: platform inspection and worker acquisition

Platform downloads are **not enabled** in the production app. `media_api.main` does
not configure a `PlatformInspector`, and `media_worker.jobs.platform_acquirer` is
`None`. Recognized platform links return `501 platform_unavailable`. Tests inject
local fakes; they make no provider requests. Phase 5A.2 must supply a network-safe
implementation before this flow can be enabled.

## Source and inspection

`classify_url` recognizes only public single-post shapes on exact YouTube,
Instagram, and X/Twitter hostnames. It produces a canonical credential-free HTTPS
URL and provider identity. Classification is not network authorization.

`PlatformInspector.inspect(source, max_metadata_bytes, timeout_seconds)` returns
`PlatformMetadata` without a local media file. The API projects only title,
duration, bounded format identifiers and properties, available resolutions, audio
availability, and a reliable source-size estimate when one can be identified.
Unknown values remain `null`. The projected response is capped at 1 MiB. A real
inspector must independently enforce that limit on its raw network response and
must honor the 15-second deadline; an API-side size check cannot stop an
implementation that reads too much before returning.

The existing inspection response keeps `video_options` and `audio_options`. For a
platform source, `media` is `null` because no media file was probed, and
`platform_inspection` holds normalized metadata. Local uploads and direct HTTPS
files retain their existing probed `media` response. Platform `original` audio is
not offered because a metadata-only response cannot guarantee a copyable codec
after acquisition.

## Inspection-to-submission binding

Successful inspection creates a random source ID and capability token in Redis.
The server stores the canonical URL, provider, bounded format IDs, and offered
output options. The token is returned in the response body and expires after
`source_ttl_seconds` (default one hour). It is never put in a URL. Job submission
looks up this server-side record using the token and validates the chosen output
against its saved options. Extra client fields such as URL, provider, or format
IDs are ignored. A stale or expired inspection must be repeated.

The RQ job copies the validated snapshot and selected output into its persisted
record, so the worker does not need the source record after queuing. It validates
the canonical URL and provider again before acquisition. The format IDs are a
selection constraint, not a promise that the provider will still serve them.
A future acquirer must recheck availability and fail safely if it changes.

## Worker acquisition and conversion

The existing RQ queue and job status enum remain unchanged. A platform job stays
in `processing` while its stage moves through `acquiring`, `converting`, and
`finalizing`. The worker passes the validated source, saved format IDs, selected
output, byte limit, deadline, progress callback, and cancellation callback to
`PlatformAcquirer.acquire`. The acquirer owns files beneath the job's `input`
directory and returns one regular local media file. The worker validates that
file and then invokes the existing `MediaEngine`, which probes and converts it,
verifies the output, and publishes atomically. The existing capability-protected
download endpoint serves the result.

The effective acquisition byte budget is the minimum of `max_input_bytes`,
`platform_max_source_bytes`, and `platform_temp_disk_budget_bytes`; all currently
default to 512 MiB. The existing output cap remains 1 GiB. The job deadline
includes acquisition and conversion. The worker removes the input directory
after success, failure, cancellation, and before a retry, so a retry cannot reuse
a partial acquisition. The acquirer must honor the passed limit and deadline
during transfer, not merely rely on the worker's post-acquisition check. Scheduled
retention and recovery cleanup remain the fallback for abandoned job directories.

## Network security boundary for 5A.2

No network extractor is connected by this milestone. Before enabling one, enforce
public-address-only egress for **every** request: page metadata, redirects,
manifests, fragments, segments, and media URLs. Resolve and validate all DNS
answers, pin the connected address while preserving TLS hostname validation, and
repeat validation for each redirect and secondary request to prevent DNS
rebinding and private-address SSRF. Apply bounded connect/read/overall timeouts,
raw metadata and media byte limits, temporary disk limits, safe filenames,
credential-free operation, and cancellation that stops active requests. Normalize
provider errors without returning headers, signed URLs, tokens, paths, or raw
extractor dictionaries to clients. Do not add cookies, private-login extraction,
DRM or CAPTCHA bypass, playlists, or live streams.

Phase 5A.2 also needs a pinned extractor dependency, API and worker image rebuilds,
provider-specific mocked cases, and controlled live verification. The worker's
acquirer registration must remain disabled until those egress controls are tested.
