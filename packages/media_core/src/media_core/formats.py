"""Accepted input demuxers and output format identities.

Playlist and concat demuxers are deliberately excluded because they can open
additional URLs or files from within an otherwise local media input.
"""

from dataclasses import dataclass

SAFE_DEMUXERS = "mov,matroska,mp3,wav,flac,ogg,avi,mpegts,asf,aac"


@dataclass(frozen=True)
class OutputFormat:
    muxer: str
    container: str


# Keys are published file extensions. FFmpeg muxer names and ffprobe container
# names are separate concepts: an .opus file is an Ogg container with Opus audio.
OUTPUT_FORMATS = {
    "mp4": OutputFormat(muxer="mp4", container="mp4"),
    "m4a": OutputFormat(muxer="ipod", container="m4a"),
    "mp3": OutputFormat(muxer="mp3", container="mp3"),
    "flac": OutputFormat(muxer="flac", container="flac"),
    "ogg": OutputFormat(muxer="ogg", container="ogg"),
    "opus": OutputFormat(muxer="opus", container="ogg"),
    "wav": OutputFormat(muxer="wav", container="wav"),
}
