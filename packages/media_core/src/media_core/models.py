"""Structured media inspection, option, and processing results."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class CoreModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class VideoStream(CoreModel):
    index: int
    codec: str
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate: int | None = None


class AudioStream(CoreModel):
    index: int
    codec: str
    sample_rate: int | None = None
    channels: int | None = None
    bitrate: int | None = None


class MediaInfo(CoreModel):
    title: str
    duration_seconds: float | None = None
    file_size: int | None = None
    container: str | None = None
    video_streams: tuple[VideoStream, ...] = ()
    audio_streams: tuple[AudioStream, ...] = ()


class VideoOption(CoreModel):
    resolution: int
    video_action: str
    audio_action: str | None
    output_format: str = "mp4"


class ProcessRequest(CoreModel):
    source_reference: str
    output_type: str
    output_format: str
    output_dir: Path
    resolution: int | None = None
    audio_bitrate_kbps: int | None = None


class ProcessingResult(CoreModel):
    path: Path | str
    output_format: str
    file_size: int
    media: MediaInfo
    video_action: str | None = None
    audio_action: str | None = None
    video_stream_index: int | None = None
    audio_stream_index: int | None = None
    audio_bitrate_kbps: int | None = None
