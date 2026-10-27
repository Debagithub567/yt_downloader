import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from media_core.engine import EngineConfig, MediaEngine, video_options
from media_core.errors import ErrorCode, MediaError
from media_core.models import MediaInfo, ProcessRequest, VideoStream
from media_core.names import safe_stem
from media_core.probe import inspect_file
from media_core.sources import LocalFileSource
from media_core.storage import LocalResultStore
import media_core.storage as storage_module


def _request(source: Path, output: Path, kind: str, fmt: str, **kwargs) -> ProcessRequest:
    return ProcessRequest(
        source_reference=source.name, output_type=kind,
        output_format=fmt, output_dir=output, **kwargs,
    )


def _packet_hashes(path: Path, stream: str) -> list[str]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_packets",
         "-show_entries", "packet=data_hash", "-show_data_hash", "sha256",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=20,
    )
    return [packet["data_hash"] for packet in json.loads(result.stdout)["packets"]]


def test_mp4_metadata_and_options(sample_media):
    info = inspect_file(sample_media["video"])
    assert info.title == "sample"
    assert info.container == "mp4"
    assert info.duration_seconds == pytest.approx(1, abs=0.1)
    assert info.file_size == sample_media["video"].stat().st_size
    assert [(s.codec, s.width, s.height, s.fps) for s in info.video_streams] == [
        ("h264", 1920, 1080, 24)
    ]
    assert [(s.codec, s.sample_rate, s.channels) for s in info.audio_streams] == [
        ("aac", 48000, 1)
    ]
    options = video_options(info)
    assert [o.resolution for o in options] == [1080, 720, 480, 360]
    assert options[0].video_action == "copy"
    assert options[1].video_action == "transcode"
    assert all(o.audio_action == "copy" for o in options)


def test_audio_only_metadata(sample_media):
    info = inspect_file(sample_media["audio"])
    assert not info.video_streams
    assert info.audio_streams[0].codec == "pcm_s16le"
    assert info.audio_streams[0].sample_rate == 48000
    assert not video_options(info)


def test_4k_options_include_all_presets_without_upscaling():
    info = MediaInfo(
        title="4k", video_streams=(VideoStream(index=0, codec="hevc", width=3840, height=2160),),
    )
    options = video_options(info)
    assert [option.resolution for option in options] == [2160, 1440, 1080, 720, 480, 360]
    assert all(option.video_action == "transcode" for option in options)


def test_1080_to_720_conversion(sample_media, tmp_path):
    result = MediaEngine().process(
        LocalFileSource(sample_media["root"]),
        _request(sample_media["video"], tmp_path, "video", "mp4", resolution=720),
    )
    output = inspect_file(result.path)
    assert result.video_action == "transcode"
    assert result.audio_action == "copy"
    assert output.video_streams[0].codec == "h264"
    assert (output.video_streams[0].width, output.video_streams[0].height) == (1280, 720)
    assert output.audio_streams[0].codec == "aac"
    assert result.file_size == result.path.stat().st_size
    assert not list(tmp_path.glob(".media-job-*"))


def test_native_video_remux_copies_streams(sample_media, tmp_path):
    result = MediaEngine().process(
        LocalFileSource(sample_media["root"]),
        _request(sample_media["video"], tmp_path, "video", "mp4", resolution=1080),
    )
    output = inspect_file(result.path)
    assert (result.video_action, result.audio_action) == ("copy", "copy")
    assert [(s.codec, s.width, s.height) for s in output.video_streams] == [("h264", 1920, 1080)]
    assert [s.codec for s in output.audio_streams] == ["aac"]
    assert result.media.title == "sample"
    assert _packet_hashes(sample_media["video"], "v:0") == _packet_hashes(result.path, "v:0")


def test_audio_extraction_to_m4a(sample_media, tmp_path):
    result = MediaEngine().process(
        LocalFileSource(sample_media["root"]),
        _request(sample_media["video"], tmp_path, "audio", "m4a"),
    )
    info = inspect_file(result.path)
    assert result.audio_action == "copy"
    assert result.path.suffix == ".m4a"
    assert not info.video_streams
    assert info.audio_streams[0].codec == "aac"


def test_incompatible_audio_is_converted_to_m4a(sample_media, tmp_path):
    result = MediaEngine().process(
        LocalFileSource(sample_media["root"]),
        _request(sample_media["audio"], tmp_path, "audio", "m4a"),
    )
    info = inspect_file(result.path)
    assert result.audio_action == "transcode"
    assert result.media.container == "m4a"
    assert not info.video_streams
    assert info.audio_streams[0].codec == "aac"


def test_mp3_conversion_and_original_audio(sample_media, tmp_path):
    source = LocalFileSource(sample_media["root"])
    mp3 = MediaEngine().process(
        source, _request(sample_media["audio"], tmp_path, "audio", "mp3", audio_bitrate_kbps=192),
    )
    original = MediaEngine().process(
        source, _request(sample_media["audio"], tmp_path, "audio", "original"),
    )
    assert (mp3.output_format, mp3.audio_action, mp3.audio_bitrate_kbps) == ("mp3", "transcode", 192)
    assert inspect_file(mp3.path).audio_streams[0].codec == "mp3"
    assert (original.output_format, original.audio_action) == ("wav", "copy")
    assert inspect_file(original.path).audio_streams[0].codec == "pcm_s16le"


def test_original_opus_preserves_codec_and_packets(sample_media, tmp_path):
    opus = tmp_path / "uploads" / "tone.opus"
    opus.parent.mkdir()
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "1", "-c:a", "libopus", str(opus)],
        check=True, timeout=30,
    )
    source = inspect_file(opus)
    assert source.container == "ogg"
    assert source.audio_streams[0].codec == "opus"

    output_dir = tmp_path / "results"
    result = MediaEngine().process(
        LocalFileSource(opus.parent), _request(opus, output_dir, "audio", "original"),
    )
    verified = inspect_file(result.path)
    assert result.path.suffix == ".opus"
    assert result.output_format == "opus"
    assert result.audio_action == "copy"
    assert verified.container == "ogg"
    assert verified.audio_streams[0].codec == "opus"
    assert not verified.video_streams
    assert _packet_hashes(opus, "a:0") == _packet_hashes(result.path, "a:0")
    assert not list(output_dir.glob(".media-job-*"))


@pytest.mark.parametrize("bitrate_kbps", [128, 192, 320])
def test_mp3_output_has_requested_bitrate(sample_media, tmp_path, bitrate_kbps):
    result = MediaEngine().process(
        LocalFileSource(sample_media["root"]),
        _request(sample_media["audio"], tmp_path, "audio", "mp3", audio_bitrate_kbps=bitrate_kbps),
    )
    verified = inspect_file(result.path)
    assert verified.container == "mp3"
    assert verified.audio_streams[0].codec == "mp3"
    assert verified.audio_streams[0].bitrate == pytest.approx(bitrate_kbps * 1000, rel=0.05)


def test_local_store_atomic_publish_and_failure_cleanup(tmp_path, monkeypatch):
    store = LocalResultStore()
    output_dir = tmp_path / "results"
    actual_replace = storage_module.os.replace
    calls = []

    def checked_replace(source, destination):
        calls.append((source, destination))
        assert source.stat().st_dev == destination.parent.stat().st_dev
        return actual_replace(source, destination)

    monkeypatch.setattr(storage_module.os, "replace", checked_replace)
    with store.workspace(output_dir) as workspace:
        partial = workspace / "output.partial"
        partial.write_bytes(b"complete")
        final = store.publish(partial, output_dir, "complete.mp3")
        assert final.read_bytes() == b"complete"
        assert not partial.exists()
    assert len(calls) == 1
    assert not list(output_dir.glob(".media-job-*"))

    def failed_replace(source, destination):
        raise OSError("forced publish failure")

    monkeypatch.setattr(storage_module.os, "replace", failed_replace)
    with pytest.raises(OSError, match="forced publish failure"):
        with store.workspace(output_dir) as workspace:
            partial = workspace / "output.partial"
            partial.write_bytes(b"incomplete")
            store.publish(partial, output_dir, "failed.mp3")
    assert not (output_dir / "failed.mp3").exists()
    assert not list(output_dir.glob(".media-job-*"))


def test_video_without_audio_and_invalid_requests(sample_media, tmp_path):
    source = LocalFileSource(sample_media["root"])
    with pytest.raises(MediaError) as exc:
        MediaEngine().process(source, _request(sample_media["silent"], tmp_path, "audio", "m4a"))
    assert exc.value.code == ErrorCode.MISSING_AUDIO
    with pytest.raises(MediaError) as exc:
        MediaEngine().process(source, _request(sample_media["video"], tmp_path, "video", "mp4", resolution=2160))
    assert exc.value.code == ErrorCode.RESOLUTION_UNAVAILABLE
    with pytest.raises(MediaError) as exc:
        MediaEngine().process(source, _request(sample_media["video"], tmp_path, "video", "avi", resolution=720))
    assert exc.value.code == ErrorCode.INVALID_SOURCE
    with pytest.raises(MediaError) as exc:
        MediaEngine().process(source, _request(sample_media["video"], tmp_path, "audio", "mp3", audio_bitrate_kbps=999))
    assert exc.value.code == ErrorCode.INVALID_SOURCE
    assert not list(tmp_path.iterdir())


def test_silent_video_remains_silent(sample_media, tmp_path):
    result = MediaEngine().process(
        LocalFileSource(sample_media["root"]),
        _request(sample_media["silent"], tmp_path, "video", "mp4", resolution=360),
    )
    output = inspect_file(result.path)
    assert output.video_streams[0].height == 360
    assert not output.audio_streams
    assert result.audio_action is None


def test_invalid_media_and_path_escape(sample_media, tmp_path):
    corrupt = sample_media["root"] / "corrupt.mp4"
    corrupt.write_bytes(b"not media")
    with pytest.raises(MediaError) as exc:
        MediaEngine().process(
            LocalFileSource(sample_media["root"]),
            _request(corrupt, tmp_path, "audio", "m4a"),
        )
    assert exc.value.code == ErrorCode.INVALID_MEDIA
    assert not list(tmp_path.iterdir())
    with pytest.raises(MediaError) as exc:
        LocalFileSource(sample_media["root"]).validate(str(tmp_path / "outside.mp4"))
    assert exc.value.code == ErrorCode.MEDIA_NOT_FOUND
    assert safe_stem(r"../../bad\name?.mp4") == "name"
    corrupt.unlink()


def test_ffmpeg_failure_timeout_and_cleanup(sample_media, tmp_path):
    bad_ffmpeg = tmp_path / "fail.sh"
    bad_ffmpeg.write_text("#!/bin/sh\necho forced failure >&2\nexit 7\n")
    bad_ffmpeg.chmod(0o755)
    source = LocalFileSource(sample_media["root"])
    output = tmp_path / "out"
    request = _request(sample_media["video"], output, "video", "mp4", resolution=720)
    with pytest.raises(MediaError) as exc:
        MediaEngine(EngineConfig(ffmpeg=str(bad_ffmpeg))).process(source, request)
    assert exc.value.code == ErrorCode.FFMPEG_FAILED
    assert "forced failure" in exc.value.details["stderr"]
    assert not list(output.iterdir())
    bad_ffmpeg.write_text("#!/bin/sh\nsleep 30\n")
    with pytest.raises(MediaError) as exc:
        MediaEngine(EngineConfig(ffmpeg=str(bad_ffmpeg), process_timeout_seconds=0.1)).process(source, request)
    assert exc.value.code == ErrorCode.PROCESSING_TIMEOUT
    assert not list(output.iterdir())


def test_ffprobe_failure_is_structured(sample_media, tmp_path):
    with pytest.raises(MediaError) as exc:
        MediaEngine(EngineConfig(ffprobe="missing-ffprobe-binary")).process(
            LocalFileSource(sample_media["root"]),
            _request(sample_media["video"], tmp_path, "video", "mp4", resolution=720),
        )
    assert exc.value.code == ErrorCode.PROBE_FAILED
    assert not list(tmp_path.iterdir())


def test_output_size_limit_is_enforced(sample_media, tmp_path):
    with pytest.raises(MediaError) as exc:
        MediaEngine(EngineConfig(max_output_bytes=1024)).process(
            LocalFileSource(sample_media["root"]),
            _request(sample_media["audio"], tmp_path, "audio", "mp3", audio_bitrate_kbps=128),
        )
    assert exc.value.code == ErrorCode.SIZE_LIMIT_EXCEEDED
    assert not list(tmp_path.iterdir())


def test_concurrent_jobs_use_separate_workspaces(sample_media, tmp_path):
    source = LocalFileSource(sample_media["root"])
    engine = MediaEngine()
    request = _request(sample_media["video"], tmp_path, "audio", "m4a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: engine.process(source, request), range(2)))
    assert results[0].path != results[1].path
    assert all(result.path.exists() and inspect_file(result.path).audio_streams for result in results)
    assert not list(tmp_path.glob(".media-job-*"))
