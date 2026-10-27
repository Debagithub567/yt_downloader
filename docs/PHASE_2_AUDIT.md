# Phase 2 implementation audit and recovery

Audit date and recovery verification: 2026-10-27. Docker runtime verification: 2026-10-27. Repository: `/Users/debajitroul/yt_downloader`. Baseline: `7c614bb` on `main` (`origin/main`). The original audit found 36/43 verified items, then the Opus recovery reached 42/43. This revision records live Docker verification. No volume was reset; no work was committed, pushed, or deployed.

## 1. Executive summary

**Verified completion: 100% (43 of 43 checklist items).** The denominator remains every individually named requirement in sections A–G of the original request: A 5, B 7, C 6, D 5, E 7, F 9, G 4. A point is counted only when its implementation was exercised by a passing test or, for documentation, directly reviewed. Docker runtime compatibility is now verified by a fresh Linux image build, container media tests and live service checks. This checklist score is not a claim that the planned Phase 3 API/job flow or production hardening is complete.

The Python library stages confined local files and allowlisted HTTPS URLs, inspects media with ffprobe, discovers streams and feasible video presets, produces MP4/M4A/MP3 and supported original-audio formats, and uses isolated workspaces with atomic local publication. The original Opus failure was reproduced and fixed by separating published extension, FFmpeg muxer, and probed container. The full **39-test** Python suite passes on the host. All checked modules import, dependencies are consistent, FFmpeg/ffprobe are present, and Compose configuration parses.

**Stability:** tested host and Linux-container paths are stable. A controlled local TLS server verified pinned-address download and certificate hostname rejection; no public media URL was contacted. All four Compose services were up after the API/worker rebuild, Linux media tests passed, API and web returned HTTP 200, Redis answered PING, the RQ worker was idle on `media`, and API↔worker shared-volume access passed. The API still has only `/api/health`, and the worker has only its RQ loop; those are planned Phase 3 integrations.

### Status key and counting

- **Implemented and tested/verified (V):** passing test covers the behavior, or documentation was inspected. For documentation, “verified” means reviewed, not executable testing.
- **Implemented but untested (U):** code/configuration exists, but the decisive behavior was not exercised in this audit or the existing tests.
- **Partially implemented (P):** the requirement has meaningful coverage but a material gap.
- **Not implemented (N):** no implementation found.
- **Broken (B):** an implemented, advertised path was reproduced failing.

Counts: **V 43, U 0, P 0, N 0, B 0 = 43**. Percentage: `43 / 43 × 100 = 100%`. Remaining risks outside the enumerated checklist are stated in sections 5 and 8.

## 2. Feature completion matrix

Paths below are relative to the repository root. “Existing test” names identify the committed or uncommitted tests actually run in this audit. No test claim implies a production service or remote site was exercised.

| Feature | Status | File / function | Test evidence | Remaining work |
| --- | --- | --- | --- | --- |
| A1 Source-adapter interface | V | `packages/media_core/src/media_core/sources.py:37` `SourceAdapter` | Both adapters used by `test_engine.py`; imports pass | None for Phase 2 contract. |
| A2 Local file support | V | `sources.py:63` `LocalFileSource` | `test_local_source_is_bounded_and_confined`; engine integration tests | Consider file-descriptor hardening if a future upload root is writable by another actor. |
| A3 Authorized direct media URL support | V | `sources.py:142` `DirectURLSource` | `test_authorized_url_download_uses_pinned_address_and_verified_tls`: controlled HTTPS transfer passes with certificate validation | Public-network behavior was not tested; no Phase 2 code gap observed. |
| A4 Input validation | V | `sources.py:70,160`; `engine.py:65` | Unsafe URL, size, path, invalid request tests pass | Validate untrusted HTTP request bodies in Phase 3 API. |
| A5 SSRF protection | V | `sources.py:125,160,210`; `formats.py:9` | Mocked private/mixed DNS and redirect tests plus controlled TLS pinned-address and wrong-hostname rejection pass | Local upload-root check/open race remains a conditional security risk, separate from URL SSRF. |
| B1 FFprobe integration | V | `probe.py:42` `inspect_file`; `commands.py:11` | `test_mp4_metadata_and_options`, `test_audio_only_metadata` | None for tested formats. |
| B2 Metadata extraction | V | `probe.py:87-105` | Duration, size, title, container asserted in `test_mp4_metadata_and_options` | Expand unusual container coverage later. |
| B3 Video stream discovery | V | `probe.py:65-73` | Video and audio-only tests | None for basic path. |
| B4 Audio stream discovery | V | `probe.py:74-82` | Video/audio-only tests | None for basic path. |
| B5 Codec identification | V | `probe.py:67,76` | H.264, AAC, PCM codec assertions | Add more codec fixtures for original-audio mapping. |
| B6 Resolution detection | V | `probe.py:68`; `engine.py:33` | 1080p metadata/options and 4K option test | Rotation/display matrix handling is untested. |
| B7 Structured metadata models | V | `models.py:8-35` | Models returned in inspection and conversion tests; imports pass | None for current contract. |
| C1 MP4 output | V | `engine.py:138-160` | `test_1080_to_720_conversion`, `test_native_video_remux_copies_streams` | None for tested formats. |
| C2 Resolution selection | V | `engine.py:33-47,142-154` | 1080→720 conversion and unavailable 2160 tests | Native resolutions outside preset list have no copy option; decide if this is desired. |
| C3 Stream copying | V | `engine.py:43-45,146-147,155-156` | Native video test compares packet hashes; M4A AAC copy test | Extend copy tests to more containers/codecs. |
| C4 Transcoding | V | `engine.py:150-154,155-156` | 1080→720 video and PCM→AAC tests | Broader codec matrix remains untested. |
| C5 H.264/AAC compatibility | V | `engine.py:146-159,185-194` | Output codecs asserted by video tests | Add non-H.264 video and non-AAC video-audio fixture. |
| C6 Prevention of unnecessary upscaling | V | `engine.py:38-47,142-144` | 4K options test; 1080→2160 request rejected | Portrait/rotation cases untested. |
| D1 M4A extraction | V | `engine.py:171-182` | `test_audio_extraction_to_m4a`, `test_incompatible_audio_is_converted_to_m4a` | None for AAC and PCM cases. |
| D2 MP3 conversion | V | `engine.py:175-180` | `test_mp3_conversion_and_original_audio` | None for tested case. |
| D3 Configurable audio bitrate | V | `engine.py:22,76-80,180` | `test_mp3_output_has_requested_bitrate` probes 128/192/320 kbps outputs, all within 5% | None for configured presets. |
| D4 Original audio preservation | V | `engine.py:50-57,165-169,185-200`; `formats.py:19-27`; `probe.py:92-97` | Opus regression verifies `.opus`, Ogg container, Opus codec, and identical audio packet hashes; WAV test and exploratory AAC/MP3/FLAC remuxes pass | Vorbis fixture could not be generated by this host FFmpeg build; keep the mapping under test on a build with an encoder. |
| D5 Missing audio handling | V | `engine.py:162-163` | `test_video_without_audio_and_invalid_requests`; silent video succeeds | None for current behavior. |
| E1 FFmpeg error handling | V | `commands.py:11-54`; `engine.py:102-105` | Forced FFmpeg exit code 7 produces structured error | Add less common encoder/disk error tests if needed. |
| E2 Timeouts | V | `commands.py:25-31`; `sources.py:180-190,208-244` | Forced FFmpeg timeout and mocked DNS timeout tests | Slow real HTTP/TLS body behavior not integration tested. |
| E3 Safe filenames | V | `names.py:8-14`; `sources.py:48-53`; `engine.py:116` | Traversal-looking name assertion and encoded URL filename test | None for tested cases. |
| E4 Unique temporary directories | V | `storage.py:18-23` | `test_concurrent_jobs_use_separate_workspaces` | None for two simultaneous local jobs. |
| E5 Atomic output publishing | V | `storage.py:25-28` `os.replace` | `test_local_store_atomic_publish_and_failure_cleanup` verifies same-device `os.replace`, published bytes, failed replacement, and workspace cleanup | Kernel atomicity follows `os.replace`; no multi-process visibility observer was run. |
| E6 Temporary-file cleanup | V | `storage.py:20-23`; `sources.py:106-121,234-256` | Failure, timeout, size-limit, concurrent tests assert no job/temp files | Direct `source.inspect` caller owns its supplied destination; document lifecycle. |
| E7 Structured errors | V | `errors.py:6-29` | Error-code assertions for missing audio, invalid input, timeout, probe/FFmpeg failure | API mapping belongs to Phase 3. |
| F1 Unit tests | V | `packages/media_core/tests/test_sources.py`, `test_engine.py`, `test_settings.py` | Included in 39-pass suite | Add tests for remaining risks as needed. |
| F2 FFprobe inspection tests | V | `test_engine.py:33-67` | MP4, audio-only, synthetic 4K tests pass | Unusual container/metadata cases untested. |
| F3 FFmpeg processing tests | V | `test_engine.py:69-95,180-216` | Conversion, remux, failure, timeout, size tests pass | Add output-completeness assertion. |
| F4 Audio conversion tests | V | `test_engine.py` | M4A, MP3, original WAV/Opus and bitrate tests pass | Vorbis fixture remains unavailable on host. |
| F5 Video resolution tests | V | `test_engine.py:60-81,135-159` | 4K options, 720 output, upscale rejection pass | Portrait/rotation cases untested. |
| F6 Invalid input tests | V | `test_engine.py:135-177`; `test_sources.py:15-49` | Invalid request/media/path/URL tests pass | Add race and malformed FFprobe-output cases if security model requires. |
| F7 SSRF security tests | V | `test_sources.py` | Mocked DNS/redirect/size/timeout and controlled real TLS tests pass | No public URL test; controlled test redirects a validated test address to loopback only inside the test. |
| F8 Concurrent processing tests | V | `test_engine.py:219-227` | Two concurrent jobs get different outputs and cleaned workspaces | Load/process isolation beyond two jobs untested. |
| F9 Docker compatibility | V | `compose.yaml`; `infra/Dockerfile.python` | Fresh API/worker build, 38/38 media-core tests in an ephemeral Linux worker container, live API/Redis/RQ checks, shared-volume round trip, and four services up | None for local Compose compatibility; this is not production deployment validation. |
| G1 Architecture updates | V | `docs/architecture.md` | Reviewed current data flow and Phase 3 boundaries | Correct any claims affected by fixes. |
| G2 Developer setup instructions | V | `README.md:6-54`; `docs/media_engine.md:21-76` | Reviewed instructions; host and Linux container commands were executed | None for local development setup. |
| G3 Media engine usage examples | V | `docs/media_engine.md:21-56` | Reviewed; examples match imported APIs; tests exercise equivalent calls | Manual example itself was not run as written. |
| G4 Known limitations | V | `README.md:70-74`; `docs/media_engine.md:13-25` | Guide lists supported original codecs, Opus/Ogg distinction, actual output checks, absent duration-completeness check, local-root requirement, and verified Linux test results | Update if behavior changes. |

### Resolution of the five originally unverified items

| Original item | Existing implementation | New verification and actual result | Current status |
| --- | --- | --- | --- |
| A3 Authorized direct URL | `DirectURLSource.stage` | Controlled HTTPS GET with a generated certificate downloaded five bytes; address passed to `connect` was the validated public test address. Passed. | V |
| A5 SSRF protection | `_validated` plus `_PinnedHTTPSConnection.connect` | Existing blocked-DNS/redirect tests passed; a real TLS handshake accepted the allowed hostname and rejected a different hostname. Passed. | V |
| D3 Audio bitrate | MP3 `-b:a` in `MediaEngine._plan` | ffprobe reported output bitrates within 5% of 128, 192, and 320 kbps. Three cases passed. | V |
| E5 Atomic publication | `LocalResultStore.publish` calls `os.replace` on the output filesystem | Success path used same-device `os.replace`; forced replacement failure published no file and removed the workspace. Passed. | V |
| F9 Docker compatibility | `compose.yaml`, `infra/Dockerfile.python` | Docker Desktop daemon 29.7.2 connected; fresh API/worker images built and recreated; 38 Linux media tests passed; API 200, Redis PONG, RQ worker idle on `media`, and shared-volume round trip passed. | V |

## 3. Test results and environment

All commands below were run from the repository root on 2026-10-27 using the existing `.venv` and project-local Python. Python test execution set `PYTHONDONTWRITEBYTECODE=1` and disabled pytest cache writes. Media fixtures and the controlled TLS certificate were generated in pytest/system temporary directories. The localhost TLS tests needed an unsandboxed test invocation because sandbox policy denied socket binding; that invocation did not contact a public host.

| Check / command | Result |
| --- | --- |
| Syntax/import heredoc shown below | 20 Python files parsed; `media_core.settings`, `sources`, `probe`, `engine`, `storage`, `media_api.main`, `media_worker.main` imported successfully. Python 3.13.18 from project `.venv`. |
| Before fix: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest packages/media_core/tests/test_engine.py::test_original_opus_preserves_codec_and_packets -p no:cacheprovider -q` | **1 failed as expected**, at the strict container comparison: actual `ogg` versus `.opus` extension. This is recorded as reproduction, not a final regression failure. |
| After fix: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest packages/media_core/tests/test_engine.py::test_original_opus_preserves_codec_and_packets packages/media_core/tests/test_engine.py::test_mp3_conversion_and_original_audio packages/media_core/tests/test_engine.py::test_audio_extraction_to_m4a packages/media_core/tests/test_engine.py::test_native_video_remux_copies_streams -p no:cacheprovider -q` | **4 passed**; Opus, MP3, M4A and MP4 paths passed together. |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest packages/media_core/tests/test_sources.py::test_authorized_url_download_uses_pinned_address_and_verified_tls packages/media_core/tests/test_sources.py::test_authorized_url_rejects_tls_hostname_mismatch -p no:cacheprovider -q` | Initial sandbox run: **2 failed to start** because binding `127.0.0.1` raised `PermissionError: [Errno 1] Operation not permitted`. Rerun outside sandbox: **2 passed**. Real TLS certificate validation was enabled; only the test socket was redirected to loopback. |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest packages/media_core/tests/test_engine.py::test_mp3_output_has_requested_bitrate packages/media_core/tests/test_engine.py::test_local_store_atomic_publish_and_failure_cleanup -p no:cacheprovider -q` | **4 passed**: three encoded bitrate presets and one atomic publish/failure cleanup test. |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest packages/media_core/tests services/api/tests -p no:cacheprovider -q` (outside sandbox for localhost TLS) | **39 passed, 0 failed, 0 skipped**, 1 warning, 2.77 s. Includes FFmpeg/ffprobe media integration, TLS, failure cleanup, concurrency, and API health unit tests. |
| `.venv/bin/python -m pip check` | `No broken requirements found.` Pip warned that its user cache directory was not writable and disabled caching; dependency validation passed. |
| `ffmpeg -version`, `ffprobe -version`, `ffmpeg -hide_banner -encoders | rg 'vorbis|opus|mp3|aac|flac'` | Host FFmpeg/ffprobe 8.0.1 at `/opt/homebrew/bin`; H.264, AAC, `libmp3lame`, `libopus`, FLAC available. `libvorbis` is absent and the native experimental Vorbis encoder failed to generate a fixture. |
| `docker compose version`; `docker compose config --quiet` | Compose v5.3.1 available; configuration validation passed. |
| Prior attempt: `docker info --format '{{.ServerVersion}}'` | Before Docker Desktop was started, the sandbox denied the socket and an unsandboxed check could not connect. This historical blocker was resolved in the Docker runtime verification below. |
| `git diff --check` | Passed, no whitespace errors in tracked changes. |
| Exploratory generated originals: AAC/M4A, MP3, FLAC, Opus, PCM/WAV | AAC, MP3, FLAC and Opus processed as `copy` and ffprobe confirmed expected codec/container; WAV is covered by existing pytest. Vorbis source generation failed due host encoder availability and remains untested on this host. |
| Exploratory generated 3 s MP4 with low output-byte limits (original audit) | Limits raised `size_limit_exceeded`; no truncated result observed. No source/output duration comparison exists, so completeness beyond these cases remains unverified. |

### Docker runtime verification (2026-10-27)

Docker commands were executed with daemon access outside the filesystem/network sandbox. All four services were already running at the start. Only `api` and `worker` were rebuilt and recreated; Redis, web and named volumes were not reset. The runtime image does not include pytest, so the test command installed it only in a disposable `docker compose run --rm` worker container.

| Exact command | Actual result |
| --- | --- |
| `docker info --format 'ServerVersion={{.ServerVersion}} OSType={{.OSType}} Architecture={{.Architecture}}'` | `ServerVersion=29.7.2 OSType=linux Architecture=aarch64`. The sandboxed invocation failed with socket permission denied; the authorized daemon invocation passed. |
| `docker compose ps --all && docker compose config --quiet` | Four services listed: `web`, `api`, `worker` up; `redis` up and healthy. Compose configuration passed. |
| `docker compose build api worker` | Both images built successfully from `infra/Dockerfile.python` with the latest media-core source. API image ID `cc986163fada`; worker image ID `64dd9227998b`. |
| `docker compose up -d --no-deps --force-recreate api worker && docker compose ps --all` | Only API and worker containers recreated and started; web and Redis stayed up. No volume reset. |
| `docker compose exec -T worker sh -c 'python --version; ffmpeg -version \| head -1; ffprobe -version \| head -1; command -v openssl \|\| true; python -m pip show pytest \| head -3'` | Python 3.12.15; FFmpeg and ffprobe 7.1.5-0+deb13u1; OpenSSL `/usr/bin/openssl`; pytest absent from the runtime image, as expected. |
| `docker compose run --rm --no-deps --user root worker sh -c 'python -m pip install --no-cache-dir "pytest>=8,<10" && python -m pytest /app/packages/media_core/tests -q -p no:cacheprovider'` | **38 passed, 0 failed, 0 skipped, 3.71 s** in an ephemeral Linux worker container. The run includes generated FFmpeg/ffprobe media tests, original Opus, controlled TLS, bitrate, concurrency and cleanup tests. Ephemeral test container was removed. |
| `curl --fail --silent --show-error --output /tmp/phase2-api-health.json --write-out 'HTTP %{http_code}\n' http://127.0.0.1:8000/api/health && cat /tmp/phase2-api-health.json` | `HTTP 200`; body `{"status":"ok"}`. |
| `docker compose exec -T redis redis-cli PING` | `PONG`. |
| `docker compose logs --tail=30 worker`; `docker compose exec -T redis redis-cli SMEMBERS rq:workers && docker compose exec -T redis redis-cli SCARD rq:workers` | Worker log says `Listening on media...`; Redis returned one registered worker. |
| `docker compose exec -T worker python -c 'from redis import Redis; from rq import Worker; from media_core.settings import Settings; r=Redis.from_url(Settings().redis_url); workers=Worker.all(connection=r); print([(w.name, w.state, [q.name for q in w.queues]) for w in workers]); assert any(w.state == "idle" and any(q.name == "media" for q in w.queues) for w in workers)'` | One RQ worker reported `idle` and listening on the `media` queue. No job was enqueued. |
| Shared-volume commands shown below | API wrote a probe to `/data`; worker read it and wrote a reply; API read the reply and removed the probe. All four commands passed. |
| `curl --fail --silent --show-error --output /dev/null --write-out 'web HTTP %{http_code}\n' http://127.0.0.1:3000/ && curl --fail --silent --show-error --output /dev/null --write-out 'api HTTP %{http_code}\n' http://127.0.0.1:8000/api/health` | `web HTTP 200`; `api HTTP 200`. |
| `docker compose ps --all && docker compose images` | Final state: all four services up, Redis healthy; API and worker containers use the just-built linux/arm64 images. |

Shared-volume commands, using the temporary probe name generated for this run:

```sh
docker compose exec -T api python -c 'from pathlib import Path; p=Path("/data/.phase2-volume-1793093932038"); p.write_text("api"); print("api wrote", p.name)'
docker compose exec -T worker python -c 'from pathlib import Path; p=Path("/data/.phase2-volume-1793093932038"); assert p.read_text()=="api"; p.write_text("worker"); print("worker read and wrote", p.name)'
docker compose exec -T api python -c 'from pathlib import Path; p=Path("/data/.phase2-volume-1793093932038"); assert p.read_text()=="worker"; print("api read", p.name)'
docker compose exec -T api python -c 'from pathlib import Path; p=Path("/data/.phase2-volume-1793093932038"); p.unlink(missing_ok=True); print("probe removed", p.name)'
```

New pytest cases: `test_original_opus_preserves_codec_and_packets` (1), two controlled TLS cases (2), `test_mp3_output_has_requested_bitrate` (3 parametrized cases), and `test_local_store_atomic_publish_and_failure_cleanup` (1): **7 new cases**. Opus/audio, MP3 bitrates, MP4/M4A codecs and video resolution are inspected with ffprobe. The Opus case compares packet hashes to prove no re-encoding. FFmpeg failure/timeout/size tests and the new publish failure test verify temporary workspace cleanup.

The syntax/import command was:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
import ast
from pathlib import Path
from importlib import import_module
paths = sorted(Path('packages').rglob('*.py')) + sorted(Path('services').rglob('*.py'))
for path in paths:
    ast.parse(path.read_text(), filename=str(path))
for name in ('media_core.settings','media_core.sources','media_core.probe','media_core.engine','media_core.storage','media_api.main','media_worker.main'):
    import_module(name)
print(f'Syntax OK: {len(paths)} Python files; 7 module imports OK')
PY
```

The sole host-suite warning is `StarletteDeprecationWarning` from `fastapi/testclient.py`: its use of `httpx` with `starlette.testclient` is deprecated and it suggests `httpx2`. The Linux media suite reported no skips or warnings. No public URL, browser UI workflow, or Phase 3 media job was tested.

## 4. Git working tree and preservation

Recent commits: `7c614bb minor changes` (HEAD and `origin/main`); `6ace8a2 initializing and structuring the project`. There are no staged changes. The existing Phase 1 and in-progress Phase 2 working tree was preserved.

**Modified tracked files (7) at final status check:** `.dockerignore`, `README.md`, `apps/web/next-env.d.ts`, `docs/architecture.md`, `infra/Dockerfile.python`, `packages/media_core/pyproject.toml`, `packages/media_core/src/media_core/sources.py`. The `apps/web/next-env.d.ts` change appeared during recovery and switches generated Next type imports from `.next/types/` to `.next/dev/types/`; this work did not edit that file, and it was left untouched.

**Untracked Phase 2 files (12):** `docs/media_engine.md`; `packages/media_core/src/media_core/{commands,engine,errors,formats,models,names,probe,storage}.py`; `packages/media_core/tests/{conftest,test_engine,test_sources}.py`. The audit report is another untracked file. No files were staged.

**Files edited during recovery:** `packages/media_core/src/media_core/formats.py`, `engine.py`, `packages/media_core/tests/test_engine.py`, `test_sources.py`, `docs/media_engine.md`, and this report. These files were already untracked before recovery, so `git diff` cannot distinguish their before/after content. Source-adapter code remains the earlier modified tracked file. The API health-only module and worker queue loop are still Phase 1 scaffolding; no media job handler or media API endpoint was added. No broken import or incomplete function body was found. The current untracked code and tests need preservation before any Git cleanup.

## 5. Critical problems, ordered by severity

1. **Medium — output completion is not explicitly checked.** `engine.py:106-114,185-200` checks nonzero size, maximum size, container, codecs and resolution, but does not compare output duration or expected stream completion with the source. The byte-limit exploration did not reproduce partial publication; this is a remaining risk, not an observed failure.
2. **Conditional security risk — local staging has a check/open gap.** `sources.py:70-90` resolves and checks a path, then `sources.py:110` opens it later. If another actor can mutate the allowed upload root concurrently, a file or symlink swap may bypass the earlier check. The root must remain private to trusted code until this is hardened. This was identified by review, not exploited in a test.
3. **Coverage limits — Vorbis, orientation and public-network behavior.** The host FFmpeg build lacks a usable Vorbis encoder for a source fixture. Portrait/rotation cases and public HTTPS service behavior remain outside current tests. Controlled TLS tests cover the URL code path without contacting a public site.

The **Opus defect is resolved**: `formats.py` maps the `.opus` extension to FFmpeg's `opus` muxer and ffprobe's `ogg` container; `engine.py` separately verifies the Opus codec and compares packet hashes in the regression test. There are **no observed broken imports, missing installed Python dependencies, final-suite failures, or incomplete function bodies**. The missing API media routes and job functions are Phase 3 work.

## 6. Remaining implementation tasks

Ordered by dependency and importance; each is outside this Docker-only verification request and can be reviewed and tested independently later.

1. **Verify output completeness and byte limits.** Add source-versus-output duration/stream checks with appropriate tolerance; test bounded outputs near `-fs` limits and failure cleanup. Explicitly handle sources without known duration.
2. **Harden local source staging if upload roots can be mutated by another actor.** Use a confined file descriptor or enforce a private-root lifecycle; test a concurrent symlink/file swap under the chosen threat model.
3. **Finish the wider format matrix.** Exercise Vorbis original-audio copy on a toolchain with a Vorbis encoder or a trusted generated fixture, plus portrait/rotation and native nonpreset video behavior. Keep unsupported cases structured.

## 7. Continuation plan

Do not rewrite the passing engine. Preserve the current uncommitted files, make each change on top of them, and verify the affected slice before broadening the suite. The Opus fix, controlled TLS tests, bitrate tests, atomic-publish test, host suite, and Docker runtime checks are complete.

| Step | Files likely to change | Expected implementation | Required tests | Definition of done |
| --- | --- | --- | --- | --- |
| 1. Output completion | `packages/media_core/src/media_core/engine.py`, `tests/test_engine.py`; `docs/media_engine.md` | Define duration tolerance and behavior for unknown duration; reject provably incomplete results before `publish`. | Near-size-limit output, short/long source, no-duration case, cleanup on rejected output. | No incomplete result is published for covered cases; docs state the actual checks. |
| 2. Confined local open | `sources.py`, `tests/test_sources.py` | Ensure the copied file is the same regular file validated inside the root, or enforce private-root ownership. | Symlink escape, race/swap, oversize and ordinary local copy. | No path outside the allowed root can be copied under the chosen threat model. |
| 3. Remaining format coverage | `tests/test_engine.py`; production files only if a defect appears | Add a portable Vorbis source fixture or use a toolchain with an encoder; clarify native nonpreset and rotated video behavior. | Original Vorbis packet-copy/codec/container test, portrait/rotation tests. | Advertised behavior succeeds or is explicitly rejected with a structured error. |

**Recommended next code task:** Step 1, output-completeness verification, which has a concrete risk but no reproduced failure yet. It is outside this Docker-only verification request.

## 8. Phase 3 readiness

The media engine passed both host and Linux-container checks and has a usable `MediaEngine.process` entry point and local `ResultStore` boundary. **Phase 2's enumerated checklist is fully verified and the engine is ready to begin Phase 3 integration.** Output-completeness and mutable-root risks should be resolved before exposing processing to untrusted inputs; controlled HTTPS transport was tested, but public-network behavior was not. Phase 3 still needs an API upload/URL boundary, request limits, job serialization and ownership, progress/cancellation/retries, result authorization, expiry, and cleanup. None of those were implemented in this Docker-only verification.
