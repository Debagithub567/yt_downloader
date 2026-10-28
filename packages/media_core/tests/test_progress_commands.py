"""Progress-pipe and subprocess lifecycle checks without network services."""

import os
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from media_core.commands import _progress_seconds, run_progress_command
import media_core.engine as engine_module
from media_core.engine import EngineConfig, MediaEngine, progress_percent
from media_core.errors import ErrorCode, MediaError
from media_core.models import ProcessRequest
from media_core.probe import inspect_file


@pytest.mark.parametrize(("line", "expected"), [
    (b"out_time_us=1500000", 1.5),
    (b"out_time_ms=2500000", 2.5),
    (b"out_time=00:01:02.500000", 62.5),
    (b"out_time_us=-100", 0),
    (b"out_time_us=broken", None),
    (b"out_time=00:00:nan", None),
    (b"progress=continue", None),
    (b"out_time_us", None),
])
def test_progress_timestamp_parsing(line, expected):
    assert _progress_seconds(line) == expected


@pytest.mark.parametrize(("seconds", "duration", "expected"), [
    (0, 10, 0), (2.5, 10, 25), (15, 10, 99), (-1, 10, 0),
    (1, None, None), (1, 0, None), (1, float("nan"), None),
    (1, float("inf"), None), (float("nan"), 10, None),
])
def test_progress_percentage_uses_reliable_duration(seconds, duration, expected):
    assert progress_percent(seconds, duration) == expected


def test_progress_pipe_ignores_malformed_lines_and_incomplete_tail():
    script = (
        "import os; "
        "os.write(1, b'out_time_us=1000000\\ninvalid\\nout_time_us=oops\\n'); "
        "os.write(1, b'out_time=00:00:02.5\\nprogress=end\\nout_time_us=999')"
    )
    observed = []
    run_progress_command([sys.executable, "-c", script], timeout=3,
                         on_progress=observed.append, should_cancel=lambda: False)
    assert observed == [1.0, 2.5]


def test_incomplete_progress_line_does_not_block_timeout():
    script = "import os,time; os.write(1, b'out_time_us=123'); time.sleep(30)"
    started = time.monotonic()
    with pytest.raises(MediaError) as error:
        run_progress_command([sys.executable, "-c", script], timeout=0.2,
                             on_progress=lambda _value: None, should_cancel=lambda: False)
    assert error.value.code == ErrorCode.PROCESSING_TIMEOUT
    assert time.monotonic() - started < 3


def test_cancellation_prevents_source_open(tmp_path):
    source = Mock()
    request = ProcessRequest(source_reference="media.wav", output_type="audio",
                             output_format="m4a", output_dir=tmp_path)
    with pytest.raises(MediaError) as error:
        MediaEngine().process(source, request, cancelled=lambda: True)
    assert error.value.code == ErrorCode.CANCELLED
    source.validate.assert_not_called()
    source.stage.assert_not_called()


def test_cancellation_during_source_staging_cleans_workspace(tmp_path):
    source = Mock()
    cancelled = threading.Event()
    source.stage.side_effect = lambda *_args: cancelled.set()
    request = ProcessRequest(source_reference="media.wav", output_type="audio",
                             output_format="m4a", output_dir=tmp_path / "output")
    with pytest.raises(MediaError) as error:
        MediaEngine().process(source, request, cancelled=cancelled.is_set)
    assert error.value.code == ErrorCode.CANCELLED
    assert source.stage.call_count == 1
    assert not list((tmp_path / "output").iterdir())


def test_total_deadline_stops_before_late_subprocess(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(engine_module, "monotonic", lambda: clock[0])
    source = Mock()
    source.stage.side_effect = lambda *_args: clock.__setitem__(0, 2.0)
    request = ProcessRequest(source_reference="media.wav", output_type="audio",
                             output_format="m4a", output_dir=tmp_path / "output")
    with pytest.raises(MediaError) as error:
        MediaEngine(EngineConfig(total_timeout_seconds=1)).process(source, request)
    assert error.value.code == ErrorCode.PROCESSING_TIMEOUT
    assert not list((tmp_path / "output").iterdir())


def test_cancellation_terminates_media_inspection(tmp_path):
    media = tmp_path / "source.wav"
    media.write_bytes(b"media")
    pidfile = tmp_path / "pid"
    probe = tmp_path / "slow_probe"
    probe.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    probe.chmod(0o755)
    with pytest.raises(MediaError) as error:
        inspect_file(media, ffprobe=str(probe), timeout=5, should_cancel=pidfile.exists)
    assert error.value.code == ErrorCode.CANCELLED
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_cancellation_sends_graceful_termination_and_reaps_child(tmp_path):
    marker = tmp_path / "terminated"
    pidfile = tmp_path / "pid"
    script = """
import os, signal, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
def terminate(_signal, _frame):
    Path(sys.argv[2]).write_text('terminated')
    sys.exit(0)
signal.signal(signal.SIGTERM, terminate)
os.write(1, b'out_time_us=1000000\\n')
while True:
    time.sleep(1)
"""
    cancelled = threading.Event()
    with pytest.raises(MediaError) as error:
        run_progress_command([sys.executable, "-u", "-c", script, str(pidfile), str(marker)],
                             timeout=5, on_progress=lambda _value: cancelled.set(),
                             should_cancel=cancelled.is_set)
    assert error.value.code == ErrorCode.CANCELLED
    assert marker.read_text() == "terminated"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_cancellation_forces_termination_after_grace(tmp_path):
    pidfile = tmp_path / "pid"
    script = """
import os, signal, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.write(1, b'out_time_us=1000000\\n')
while True:
    time.sleep(1)
"""
    cancelled = threading.Event()
    started = time.monotonic()
    with pytest.raises(MediaError) as error:
        run_progress_command([sys.executable, "-u", "-c", script, str(pidfile)],
                             timeout=5, on_progress=lambda _value: cancelled.set(),
                             should_cancel=cancelled.is_set)
    assert error.value.code == ErrorCode.CANCELLED
    assert time.monotonic() - started < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
