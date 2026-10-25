"""Contract for authorized media sources.

Concrete upload and direct-URL adapters arrive with the media engine in Phase 2.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class StagedMedia:
    path: Path
    filename: str


class SourceAdapter(Protocol):
    """Validate and stage an input before shared ffprobe/FFmpeg processing."""

    def validate(self, reference: str) -> None:
        """Raise a source-specific error if the reference is not permitted."""

    def stage(self, reference: str, destination: Path) -> StagedMedia:
        """Copy permitted media into an isolated staging directory."""

