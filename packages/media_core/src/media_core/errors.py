"""Machine-readable failures shared by source adapters and the media engine."""

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_SOURCE = "invalid_source"
    UNAUTHORIZED_SOURCE = "unauthorized_source"
    MEDIA_NOT_FOUND = "media_not_found"
    INVALID_MEDIA = "invalid_media"
    UNSUPPORTED_CODEC = "unsupported_codec"
    RESOLUTION_UNAVAILABLE = "resolution_unavailable"
    MISSING_AUDIO = "missing_audio"
    PROBE_FAILED = "probe_failed"
    FFMPEG_FAILED = "ffmpeg_failed"
    PROCESSING_TIMEOUT = "processing_timeout"
    SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"
    INSUFFICIENT_DISK_SPACE = "insufficient_disk_space"
    INTERNAL_ERROR = "internal_error"


class MediaError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code.value, "message": str(self), "details": self.details}
