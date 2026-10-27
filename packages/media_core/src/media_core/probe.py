"""Inspect local media with ffprobe; external protocols are disabled."""

import json
import math
from fractions import Fraction
from pathlib import Path

from .commands import run_command
from .errors import ErrorCode, MediaError
from .formats import SAFE_DEMUXERS
from .models import AudioStream, MediaInfo, VideoStream
from .names import safe_stem


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _positive_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _fps(stream: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            value = float(Fraction(str(stream.get(key, "0"))))
        except (ValueError, ZeroDivisionError):
            continue
        if math.isfinite(value) and value > 0:
            return round(value, 3)
    return None


def inspect_file(path: Path, *, ffprobe: str = "ffprobe", timeout: float = 20) -> MediaInfo:
    if not path.is_file():
        raise MediaError(ErrorCode.MEDIA_NOT_FOUND, "Media file was not found")
    try:
        stdout, _ = run_command(
            [ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
             "-format_whitelist", SAFE_DEMUXERS,
             "-show_format", "-show_streams", "-of", "json", "-i", str(path)],
            timeout=timeout, failure=ErrorCode.PROBE_FAILED,
        )
    except MediaError as exc:
        stderr = str(exc.details.get("stderr", ""))
        if exc.code == ErrorCode.PROBE_FAILED and (
            "Invalid data found" in stderr or "moov atom not found" in stderr
        ):
            raise MediaError(ErrorCode.INVALID_MEDIA, "Source is not a valid media file") from exc
        raise
    try:
        data = json.loads(stdout)
        streams = data.get("streams", [])
        media_format = data.get("format", {})
        if not isinstance(streams, list) or not isinstance(media_format, dict):
            raise ValueError("Unexpected ffprobe result")
        video = tuple(
            VideoStream(
                index=int(s["index"]), codec=str(s.get("codec_name") or "unknown"),
                width=_positive_int(s.get("width")), height=_positive_int(s.get("height")),
                fps=_fps(s), bitrate=_positive_int(s.get("bit_rate")),
            )
            for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        )
        audio = tuple(
            AudioStream(
                index=int(s["index"]), codec=str(s.get("codec_name") or "unknown"),
                sample_rate=_positive_int(s.get("sample_rate")),
                channels=_positive_int(s.get("channels")),
                bitrate=_positive_int(s.get("bit_rate")),
            )
            for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise MediaError(ErrorCode.INVALID_MEDIA, "ffprobe returned malformed metadata") from exc
    if not video and not audio:
        raise MediaError(ErrorCode.INVALID_MEDIA, "Media has no audio or video streams")
    names = str(media_format.get("format_name") or "").split(",")
    suffix = path.suffix.lower().lstrip(".")
    tags = media_format.get("tags") or {}
    if not isinstance(tags, dict):
        tags = {}
    if suffix in ("mp4", "m4a", "mov") and "mov" in names:
        container = suffix
    elif "mov" in names:
        container = "m4a" if str(tags.get("major_brand", "")).strip().upper() == "M4A" else "mp4"
    else:
        container = suffix if suffix in names else (names[0] or None)
    title = safe_stem(str(tags.get("title") or path.name))
    return MediaInfo(
        title=title,
        duration_seconds=_positive_float(media_format.get("duration")),
        file_size=path.stat().st_size,
        container=container,
        video_streams=video,
        audio_streams=audio,
    )
