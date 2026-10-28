"""FFmpeg processing over staged, authorized media."""

import errno
import math
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from time import monotonic

from .commands import run_command, run_progress_command
from .errors import ErrorCode, MediaError
from .formats import OUTPUT_FORMATS, SAFE_DEMUXERS
from .models import AudioStream, MediaInfo, ProcessRequest, ProcessingResult, VideoOption
from .names import safe_stem
from .probe import inspect_file
from .sources import SourceAdapter
from .storage import LocalResultStore, ResultStore


@dataclass(frozen=True)
class EngineConfig:
    resolutions: tuple[int, ...] = (2160, 1440, 1080, 720, 480, 360)
    mp3_bitrates_kbps: tuple[int, ...] = (128, 192, 320)
    max_output_bytes: int = 1024 * 1024 * 1024
    probe_timeout_seconds: float = 20
    process_timeout_seconds: float = 300
    total_timeout_seconds: float | None = None
    ffmpeg_threads: int = 2
    video_preset: str = "veryfast"
    video_crf: int = 23
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


def progress_percent(seconds: float, duration: float | None) -> int | None:
    if duration is None or not math.isfinite(duration) or duration <= 0 or not math.isfinite(seconds):
        return None
    ratio = seconds / duration
    return 99 if ratio >= 0.99 else max(0, int(ratio * 100))


def video_options(media: MediaInfo, config: EngineConfig | None = None) -> tuple[VideoOption, ...]:
    config = config or EngineConfig()
    video = next((s for s in media.video_streams if s.width and s.height), None)
    if video is None:
        return ()
    native = min(video.width, video.height)
    audio = media.audio_streams[0] if media.audio_streams else None
    return tuple(
        VideoOption(
            resolution=height,
            video_action="copy" if height == native and video.codec == "h264" else "transcode",
            audio_action=("copy" if audio.codec == "aac" else "transcode") if audio else None,
        )
        for height in config.resolutions if height <= native
    )


_ORIGINAL_AUDIO = {
    "aac": "m4a",
    "mp3": "mp3",
    "flac": "flac",
    "vorbis": "ogg",
    "opus": "opus",
    "pcm_s16le": "wav",
}


class MediaEngine:
    def __init__(self, config: EngineConfig | None = None, *, storage: ResultStore | None = None):
        self.config = config or EngineConfig()
        self.storage = storage or LocalResultStore()

    def _validate(self, request: ProcessRequest) -> None:
        if not request.source_reference:
            raise MediaError(ErrorCode.INVALID_SOURCE, "A source reference is required")
        if request.output_type == "video":
            if request.output_format != "mp4" or request.audio_bitrate_kbps is not None:
                raise MediaError(ErrorCode.INVALID_SOURCE, "Video output supports MP4 without an audio bitrate")
            if request.resolution not in self.config.resolutions:
                raise MediaError(ErrorCode.RESOLUTION_UNAVAILABLE, "Unsupported target resolution")
        elif request.output_type == "audio":
            if request.output_format not in ("m4a", "mp3", "original") or request.resolution is not None:
                raise MediaError(ErrorCode.INVALID_SOURCE, "Unsupported audio output request")
            if request.output_format == "mp3":
                if request.audio_bitrate_kbps not in self.config.mp3_bitrates_kbps:
                    raise MediaError(ErrorCode.INVALID_SOURCE, "Unsupported MP3 output bitrate")
            elif request.audio_bitrate_kbps is not None:
                raise MediaError(ErrorCode.INVALID_SOURCE, "Audio bitrate is only supported for MP3")
        else:
            raise MediaError(ErrorCode.INVALID_SOURCE, "Output type must be video or audio")

    def process(
        self, source: SourceAdapter, request: ProcessRequest, *,
        progress: Callable[[int | None, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ProcessingResult:
        self._validate(request)
        observe = progress is not None or cancelled is not None
        cancelled = cancelled or (lambda: False)
        deadline = (monotonic() + self.config.total_timeout_seconds
                    if self.config.total_timeout_seconds is not None else None)
        def check_cancelled() -> None:
            if cancelled():
                raise MediaError(ErrorCode.CANCELLED, "Processing was cancelled")
            if deadline is not None and monotonic() >= deadline:
                raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "Media processing timed out")

        def remaining(configured: float) -> float:
            check_cancelled()
            return min(configured, max(0.01, deadline - monotonic())) if deadline is not None else configured

        check_cancelled()
        source.validate(request.source_reference)
        check_cancelled()
        try:
            with self.storage.workspace(request.output_dir) as workspace:
                check_cancelled()
                if progress:
                    progress(None, "staging")
                staged = source.stage(request.source_reference, workspace)
                check_cancelled()
                if progress:
                    progress(None, "inspecting")
                media = inspect_file(
                    staged.path, ffprobe=self.config.ffprobe,
                    timeout=remaining(self.config.probe_timeout_seconds), should_cancel=cancelled,
                )
                args, output_format, video_action, audio_action, video_index, audio_index = self._plan(
                    staged.path, media, request,
                )
                check_cancelled()
                required = min(self.config.max_output_bytes, max(16 * 1024 * 1024, staged.path.stat().st_size * 2))
                if shutil.disk_usage(request.output_dir).free < required:
                    raise MediaError(ErrorCode.INSUFFICIENT_DISK_SPACE, "Not enough free space for output")
                partial = workspace / "output.partial"
                args += ["-fs", str(self.config.max_output_bytes), "-f", OUTPUT_FORMATS[output_format].muxer, str(partial)]
                if observe:
                    args[1:1] = ["-progress", "pipe:1", "-stats_period", "0.5", "-nostats"]
                    duration = media.duration_seconds
                    if progress:
                        progress(progress_percent(0, duration), "processing")
                    def on_time(seconds: float) -> None:
                        if progress:
                            progress(progress_percent(seconds, duration), "processing")
                    run_progress_command(
                        args, timeout=remaining(self.config.process_timeout_seconds),
                        on_progress=on_time, should_cancel=cancelled,
                    )
                else:
                    run_command(args, timeout=remaining(self.config.process_timeout_seconds), failure=ErrorCode.FFMPEG_FAILED)
                check_cancelled()
                if progress:
                    progress(None, "finalizing")
                size = partial.stat().st_size if partial.exists() else 0
                if size == 0:
                    raise MediaError(ErrorCode.FFMPEG_FAILED, "FFmpeg produced no output")
                if size > self.config.max_output_bytes:
                    raise MediaError(ErrorCode.SIZE_LIMIT_EXCEEDED, "Output exceeds the size limit")
                verified = inspect_file(
                    partial, ffprobe=self.config.ffprobe,
                    timeout=remaining(self.config.probe_timeout_seconds), should_cancel=cancelled,
                )
                self._verify_output(verified, request, output_format, media)
                if verified.container != OUTPUT_FORMATS[output_format].container:
                    raise MediaError(ErrorCode.FFMPEG_FAILED, "Output container does not match the request")
                verified = verified.model_copy(update={"title": media.title})
                filename = f"{safe_stem(media.title)}-{uuid.uuid4().hex}.{output_format}"
                check_cancelled()
                final = self.storage.publish(partial, request.output_dir, filename)
                return ProcessingResult(
                    path=final, output_format=output_format, file_size=size, media=verified,
                    video_action=video_action, audio_action=audio_action,
                    video_stream_index=video_index, audio_stream_index=audio_index,
                    audio_bitrate_kbps=request.audio_bitrate_kbps,
                )
        except OSError as exc:
            code = ErrorCode.INSUFFICIENT_DISK_SPACE if exc.errno == errno.ENOSPC else ErrorCode.INTERNAL_ERROR
            raise MediaError(code, "Media processing could not complete", details={"reason": str(exc)}) from exc

    def _plan(
        self, path: Path, media: MediaInfo, request: ProcessRequest,
    ) -> tuple[list[str], str, str | None, str | None, int | None, int | None]:
        args = [
            self.config.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-protocol_whitelist", "file,pipe", "-format_whitelist", SAFE_DEMUXERS,
            "-i", str(path),
            "-threads", str(self.config.ffmpeg_threads), "-sn", "-dn",
        ]
        audio: AudioStream | None = media.audio_streams[0] if media.audio_streams else None
        if request.output_type == "video":
            video = next((s for s in media.video_streams if s.width and s.height), None)
            if video is None:
                raise MediaError(ErrorCode.UNSUPPORTED_CODEC, "Source has no usable video stream")
            option = next((o for o in video_options(media, self.config) if o.resolution == request.resolution), None)
            if option is None:
                raise MediaError(ErrorCode.RESOLUTION_UNAVAILABLE, "Requested resolution exceeds the source")
            args += ["-map", f"0:{video.index}"]
            if option.video_action == "copy":
                args += ["-c:v", "copy"]
            else:
                assert request.resolution is not None
                scale = f"scale=-2:{request.resolution}" if video.width >= video.height else f"scale={request.resolution}:-2"
                args += [
                    "-vf", scale, "-c:v", "libx264", "-preset", self.config.video_preset,
                    "-crf", str(self.config.video_crf), "-pix_fmt", "yuv420p",
                ]
            if audio:
                args += ["-map", f"0:{audio.index}", "-c:a", "copy" if option.audio_action == "copy" else "aac"]
            else:
                args += ["-an"]
            args += ["-movflags", "+faststart"]
            return args, "mp4", option.video_action, option.audio_action, video.index, audio.index if audio else None

        if audio is None:
            raise MediaError(ErrorCode.MISSING_AUDIO, "Source has no audio stream")
        args += ["-map", f"0:{audio.index}", "-vn"]
        if request.output_format == "original":
            original = _ORIGINAL_AUDIO.get(audio.codec)
            if original is None:
                raise MediaError(ErrorCode.UNSUPPORTED_CODEC, "Original audio codec cannot be muxed safely")
            output_format = original
            action = "copy"
        elif request.output_format == "m4a":
            output_format = "m4a"
            action = "copy" if audio.codec == "aac" else "transcode"
        else:
            output_format = "mp3"
            action = "transcode"
        if action == "copy":
            args += ["-c:a", "copy"]
        elif output_format == "mp3":
            args += ["-c:a", "libmp3lame", "-b:a", f"{request.audio_bitrate_kbps}k"]
        else:
            args += ["-c:a", "aac"]
        return args, output_format, None, action, None, audio.index

    @staticmethod
    def _verify_output(output: MediaInfo, request: ProcessRequest, output_format: str, source: MediaInfo) -> None:
        if request.output_type == "video":
            if not output.video_streams or output.video_streams[0].codec != "h264":
                raise MediaError(ErrorCode.FFMPEG_FAILED, "Output is missing H.264 video")
            video = output.video_streams[0]
            if not video.width or not video.height or min(video.width, video.height) != request.resolution:
                raise MediaError(ErrorCode.FFMPEG_FAILED, "Output resolution does not match the request")
            if source.audio_streams and (not output.audio_streams or output.audio_streams[0].codec != "aac"):
                raise MediaError(ErrorCode.FFMPEG_FAILED, "Output is missing AAC audio")
        else:
            if output.video_streams or not output.audio_streams:
                raise MediaError(ErrorCode.FFMPEG_FAILED, "Audio output contains unexpected streams")
            expected = "aac" if output_format == "m4a" else "mp3" if output_format == "mp3" else source.audio_streams[0].codec
            if output.audio_streams[0].codec != expected:
                raise MediaError(ErrorCode.FFMPEG_FAILED, "Output audio codec does not match the request")
