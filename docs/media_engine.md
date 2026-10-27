# Media engine developer guide

The Phase 2 engine is a Python library in `packages/media_core`. It does not expose media API endpoints or enqueue RQ jobs. The caller must establish that the user owns or is authorized to process the media.

## Components

- `LocalFileSource(allowed_root)` accepts only regular files within a trusted upload directory.
- `DirectURLSource(allowed_hosts)` accepts exact allowlisted HTTPS hostnames on port 443. It rejects credentials, IP literals, private or non-global DNS answers, and redirects that fail the same checks. Each TLS connection is pinned to a validated DNS address. It limits download bytes and elapsed time. Direct outbound TLS access is required; this adapter does not use an HTTP proxy.
- `inspect_file` uses ffprobe to return duration, size, container, codec and stream details. `video_options` lists only configured resolutions at or below the source's smaller dimension and marks copy versus transcode actions.
- `MediaEngine.process` stages a source in a unique work directory, selects streams, runs FFmpeg without a shell and with a timeout and output-byte cap, probes the output's container, streams, codecs, and requested resolution, then publishes it atomically through `ResultStore`. The default `LocalResultStore` uses the shared local volume.
- `MediaError.as_dict()` returns a stable code, message, and details for later API translation.

The engine permits common self-contained audio/video demuxers. Playlists and concat inputs are excluded because they may reference further network or local resources. A local adapter limit does not replace a future API's HTTP upload limit. No DRM or provider-specific extraction is included.

## Output behavior

MP4 uses H.264 video and AAC audio. Compatible native streams are copied; lower resolutions or incompatible codecs are transcoded. The default resolution presets are 2160, 1440, 1080, 720, 480, and 360 pixels; a preset is offered only if the source permits it. A native size outside those presets has no native copy option. M4A copies AAC or converts other audio to AAC. MP3 uses FFmpeg's `libmp3lame` at a configured output bitrate of 128, 192, or 320 kbps. A higher output bitrate does not add detail missing from the source. Videos without audio raise `missing_audio` for audio requests.

`original` copies the selected audio stream without re-encoding when its codec has a safe output mapping: AAC → `.m4a`, MP3 → `.mp3`, FLAC → `.flac`, Vorbis → `.ogg`, Opus → `.opus`, or PCM signed 16-bit → `.wav`. The output extension, FFmpeg muxer, and probed container are checked separately; `.opus` carries Opus audio in an Ogg container. Unsupported original codecs raise `unsupported_codec`. Tests verify original Opus packets are preserved; this host FFmpeg build could not generate a Vorbis sample, so that mapping has not been exercised here.

The default engine limits are a 1 GiB output, 20-second ffprobe calls, a 300-second FFmpeg call, and two FFmpeg threads. Source adapters default to a 512 MiB input. `EngineConfig` and adapter constructor arguments can narrow these limits. The source allowlist and output directory must be chosen by trusted application code, never directly from an unvalidated HTTP request.

The engine does not compare source and output durations to prove that every input frame was processed. `-fs` enforces an FFmpeg byte limit and the engine rejects files larger than its configured output limit, but successful probing alone is not a completeness guarantee. The local source root must remain private to trusted application code while files are staged; the current path check and later file open are separate operations. `SourceAdapter.inspect` leaves its staged copy in the caller-supplied destination, which the caller must clean up. Live public HTTPS downloads, API media routes, RQ media jobs, and remote object storage are outside the verified Phase 2 behavior.

Linux container verification passed: 38/38 media-core tests, all four Compose services running, API health returning HTTP 200, Redis responding to PING, an RQ worker listening on the `media` queue, and successful API-to-worker shared-volume read/write checks. No media job was enqueued. See [the Phase 2 audit](PHASE_2_AUDIT.md) for the executed commands and results.

## Manual local test

From the repository root, with FFmpeg, ffprobe, and the Python dependencies installed:

```sh
source .venv/bin/activate
mkdir -p /tmp/media-engine-demo/uploads /tmp/media-engine-demo/results
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i testsrc2=size=1920x1080:rate=24 \
  -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -t 1 -c:v libx264 -preset ultrafast -c:a aac \
  /tmp/media-engine-demo/uploads/sample.mp4
python - <<'PY'
from pathlib import Path
from media_core.engine import MediaEngine, video_options
from media_core.models import ProcessRequest
from media_core.sources import LocalFileSource

root = Path("/tmp/media-engine-demo")
source = LocalFileSource(root / "uploads")
media = source.inspect("sample.mp4", root / "inspection")
print(media.model_dump_json(indent=2))
print([option.model_dump() for option in video_options(media)])
result = MediaEngine().process(
    source,
    ProcessRequest(
        source_reference="sample.mp4", output_type="video", output_format="mp4",
        resolution=720, output_dir=root / "results",
    ),
)
print(result.model_dump_json(indent=2))
PY
ffprobe -v error -show_streams -show_format /tmp/media-engine-demo/results/*.mp4
```

For an authorized direct URL, construct `DirectURLSource({"media.example"})` with the exact permitted hostname and pass its HTTPS URL as `source_reference`. Automated tests use a controlled local TLS server with a test-only address redirect; they do not contact a public media site.

## Automated checks

```sh
source .venv/bin/activate
python -m pytest
python -m pip check
docker compose config --quiet
docker compose build api worker
docker compose up -d
docker compose run --rm --no-deps --user root worker sh -c 'python -m pip install --no-cache-dir "pytest>=8,<10" && python -m pytest /app/packages/media_core/tests -q'
```

The final command runs the media tests in an ephemeral Linux worker container and does not add pytest to the runtime image. It does not remove the existing Redis or media volumes.
