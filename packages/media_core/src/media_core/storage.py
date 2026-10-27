"""Local result storage boundary for the current shared-volume deployment."""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import ContextManager, Iterator, Protocol


class ResultStore(Protocol):
    def workspace(self, output_dir: Path) -> ContextManager[Path]:
        """Provide an isolated work directory on the output filesystem."""

    def publish(self, partial: Path, output_dir: Path, filename: str) -> Path | str:
        """Publish a completed file atomically and return its location."""


class LocalResultStore:
    @contextmanager
    def workspace(self, output_dir: Path) -> Iterator[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".media-job-", dir=output_dir) as work:
            yield Path(work)

    def publish(self, partial: Path, output_dir: Path, filename: str) -> Path:
        final = output_dir / filename
        os.replace(partial, final)
        return final
