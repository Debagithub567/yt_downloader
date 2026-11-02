"""Strict platform-page classification and bounded, public metadata projection.

Classification does not authorize network access. A platform extractor must use a
separately enforced egress policy before it can be connected to the live API.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qsl, urlsplit

from .errors import ErrorCode, MediaError
from .names import safe_stem
from .sources import StagedMedia


class SourceKind(StrEnum):
    UPLOAD = "upload"
    DIRECT_URL = "direct_url"
    PLATFORM = "platform"


class Platform(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    X = "x"


@dataclass(frozen=True)
class ClassifiedURL:
    kind: SourceKind
    url: str
    platform: Platform | None = None


_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_INSTAGRAM_ID = re.compile(r"[A-Za-z0-9_-]{5,32}\Z")
_X_USER = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
_X_STATUS = re.compile(r"[0-9]{1,20}\Z")
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_X_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})


def classify_url(reference: str) -> ClassifiedURL:
    if (not isinstance(reference, str) or not reference or len(reference) > 2048 or
            any(ord(char) < 33 or ord(char) == 127 for char in reference) or
            "\\" in reference):
        raise MediaError(ErrorCode.INVALID_SOURCE, "Malformed media URL")
    try:
        parsed = urlsplit(reference)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise MediaError(ErrorCode.INVALID_SOURCE, "Malformed media URL") from exc
    if (parsed.scheme != "https" or not host or port not in (None, 443) or
            parsed.username is not None or parsed.password is not None or parsed.fragment or
            host.endswith(".")):
        raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "Only credential-free HTTPS media URLs are supported")
    host = host.lower()
    platform = (Platform.YOUTUBE if host in _YOUTUBE_HOSTS else
                Platform.INSTAGRAM if host in _INSTAGRAM_HOSTS else
                Platform.X if host in _X_HOSTS else None)
    if platform is None:
        return ClassifiedURL(SourceKind.DIRECT_URL, reference)
    if "%" in parsed.path or "//" in parsed.path:
        raise MediaError(ErrorCode.INVALID_SOURCE, "Unsupported platform URL path")
    segments = [part for part in parsed.path.split("/") if part]
    if platform == Platform.YOUTUBE:
        if host == "youtu.be":
            identifier = segments[0] if len(segments) == 1 else ""
        elif segments == ["watch"]:
            try:
                values = [value for key, value in parse_qsl(parsed.query, max_num_fields=8) if key == "v"]
            except ValueError as exc:
                raise MediaError(ErrorCode.INVALID_SOURCE, "Unsupported YouTube URL") from exc
            identifier = values[0] if len(values) == 1 else ""
        elif len(segments) == 2 and segments[0] in {"shorts", "live"}:
            identifier = segments[1]
        else:
            identifier = ""
        if not _YOUTUBE_ID.fullmatch(identifier):
            raise MediaError(ErrorCode.INVALID_SOURCE, "Unsupported YouTube URL")
        return ClassifiedURL(SourceKind.PLATFORM, f"https://www.youtube.com/watch?v={identifier}", platform)
    if platform == Platform.INSTAGRAM:
        if len(segments) != 2 or segments[0] not in {"reel", "p", "tv"} or not _INSTAGRAM_ID.fullmatch(segments[1]):
            raise MediaError(ErrorCode.INVALID_SOURCE, "Unsupported Instagram URL")
        return ClassifiedURL(SourceKind.PLATFORM, f"https://www.instagram.com/{segments[0]}/{segments[1]}/", platform)
    if len(segments) != 3 or not _X_USER.fullmatch(segments[0]) or segments[1] != "status" or not _X_STATUS.fullmatch(segments[2]):
        raise MediaError(ErrorCode.INVALID_SOURCE, "Unsupported X/Twitter URL")
    return ClassifiedURL(SourceKind.PLATFORM, f"https://x.com/{segments[0]}/status/{segments[2]}", platform)


@dataclass(frozen=True)
class PlatformFormat:
    format_id: str
    extension: str | None
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    size_bytes: int | None
    size_is_estimate: bool


@dataclass(frozen=True)
class PlatformMetadata:
    platform: Platform
    title: str | None
    duration_seconds: float | None
    formats: tuple[PlatformFormat, ...]
    available_resolutions: tuple[int, ...]
    has_audio: bool
    availability: str


@dataclass(frozen=True)
class PlatformInspection:
    """Public, bounded inspection; no media file or extractor request data."""

    source: ClassifiedURL
    metadata: PlatformMetadata


def platform_snapshot(inspection: PlatformInspection) -> dict[str, Any]:
    """Persist only server-validated identity and selectable format identifiers."""
    source, metadata = inspection.source, inspection.metadata
    if source.kind != SourceKind.PLATFORM or source.platform != metadata.platform:
        raise MediaError(ErrorCode.INVALID_SOURCE, "Platform inspection identity does not match")
    canonical = classify_url(source.url)
    if canonical != source:
        raise MediaError(ErrorCode.INVALID_SOURCE, "Platform inspection identity is not canonical")
    if (metadata.availability != "public" or
            metadata.title is not None and (not isinstance(metadata.title, str) or
                                            len(metadata.title) > 160 or metadata.title != safe_stem(metadata.title)) or
            metadata.duration_seconds is not None and (
                not isinstance(metadata.duration_seconds, (int, float)) or
                not math.isfinite(metadata.duration_seconds) or metadata.duration_seconds <= 0) or
            not isinstance(metadata.formats, tuple) or len(metadata.formats) > 64 or
            any(not isinstance(item, PlatformFormat) or
                not isinstance(item.format_id, str) or
                not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item.format_id) or
                item.width is not None and (not isinstance(item.width, int) or not 0 < item.width <= 16384) or
                item.height is not None and (not isinstance(item.height, int) or not 0 < item.height <= 16384)
                for item in metadata.formats) or
            metadata.available_resolutions != tuple(sorted(
                {item.height for item in metadata.formats if item.video_codec and item.height}, reverse=True)) or
            metadata.has_audio != any(item.audio_codec for item in metadata.formats)):
        raise MediaError(ErrorCode.INVALID_MEDIA, "Platform formats are invalid")
    return {
        "url": source.url, "platform": source.platform.value,
        "format_ids": [item.format_id for item in metadata.formats],
    }


def source_from_snapshot(snapshot: dict[str, Any]) -> ClassifiedURL:
    """Fail closed if persisted job identity or format identifiers are altered."""
    if not isinstance(snapshot, dict) or set(snapshot) != {"url", "platform", "format_ids"}:
        raise MediaError(ErrorCode.INVALID_SOURCE, "Platform job source is invalid")
    url, platform, identifiers = snapshot["url"], snapshot["platform"], snapshot["format_ids"]
    if (not isinstance(url, str) or not isinstance(platform, str) or
            not isinstance(identifiers, list) or len(identifiers) > 64 or
            any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item)
                for item in identifiers)):
        raise MediaError(ErrorCode.INVALID_SOURCE, "Platform job source is invalid")
    source = classify_url(url)
    if source.kind != SourceKind.PLATFORM or source.url != url or source.platform.value != platform:
        raise MediaError(ErrorCode.INVALID_SOURCE, "Platform job source is invalid")
    return source


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 < number <= 10**12 else None


def _codec(value: Any) -> str | None:
    if not isinstance(value, str) or value in {"", "none"}:
        return None
    return value[:40] if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", value) else None


def normalize_metadata(platform: Platform, info: dict[str, Any]) -> PlatformMetadata:
    """Project extractor data without returning media URLs, headers, or raw metadata."""
    if not isinstance(info, dict) or info.get("_type") not in (None, "video"):
        raise MediaError(ErrorCode.INVALID_MEDIA, "Platform item is not a single video")
    availability = info.get("availability")
    if availability not in (None, "public") or info.get("is_live") is True:
        raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "Platform media is unavailable")
    title = info.get("title")
    title = safe_stem(title[:160]) if isinstance(title, str) and title.strip() else None
    duration = info.get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError, OverflowError):
        duration = None
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        duration = None
    raw_formats = info.get("formats")
    if not isinstance(raw_formats, list):
        raw_formats = []
    formats: list[PlatformFormat] = []
    for raw in raw_formats[:64]:
        if not isinstance(raw, dict):
            continue
        identifier = raw.get("format_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", identifier):
            continue
        video, audio = _codec(raw.get("vcodec")), _codec(raw.get("acodec"))
        if not video and not audio:
            continue
        exact = _positive_int(raw.get("filesize"))
        estimate = None if exact else _positive_int(raw.get("filesize_approx"))
        extension = raw.get("ext")
        extension = extension if isinstance(extension, str) and re.fullmatch(r"[a-z0-9]{1,8}", extension) else None
        formats.append(PlatformFormat(
            identifier, extension, video, audio, _positive_int(raw.get("width")),
            _positive_int(raw.get("height")), exact or estimate, exact is None and estimate is not None,
        ))
    return PlatformMetadata(
        platform, title, duration, tuple(formats),
        tuple(sorted({item.height for item in formats if item.video_codec and item.height}, reverse=True)),
        any(item.audio_codec for item in formats), "public",
    )


class PlatformInspector(Protocol):
    """Return metadata only; an implementation must bound every outbound request."""

    def inspect(self, source: ClassifiedURL, *, max_metadata_bytes: int,
                timeout_seconds: float) -> PlatformMetadata:
        ...


class PlatformAcquirer(Protocol):
    """Worker-only acquisition; an implementation must enforce egress and budgets."""

    def acquire(self, source: ClassifiedURL, format_ids: tuple[str, ...],
                selection: dict[str, Any], destination: Path, *,
                max_bytes: int, deadline: float, progress: Callable[[int | None], None],
                cancelled: Callable[[], bool]) -> StagedMedia:
        ...
