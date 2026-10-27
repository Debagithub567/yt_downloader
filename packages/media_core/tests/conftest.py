"""Small, locally generated media; no network or external media sites."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def sample_media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for media integration tests")
    root = tmp_path_factory.mktemp("media")
    samples = {
        "root": root,
        "video": root / "sample.mp4",
        "silent": root / "silent.mp4",
        "audio": root / "tone.wav",
    }
    for name, inputs, codecs in (
        (
            "video",
            ["-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"],
            ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"],
        ),
        (
            "silent",
            ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24"],
            ["-c:v", "libx264", "-preset", "ultrafast", "-an"],
        ),
        (
            "audio",
            ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"],
            ["-c:a", "pcm_s16le"],
        ),
    ):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             *inputs, "-t", "1", "-threads", "2", *codecs, str(samples[name])],
            check=True, timeout=60,
        )
    return samples
