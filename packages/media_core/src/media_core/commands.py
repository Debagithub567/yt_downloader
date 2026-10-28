"""Run media tools without a shell and terminate their process groups on failure."""

import os
import math
import select
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable

from .errors import ErrorCode, MediaError


def run_command(
    args: list[str], *, timeout: float, failure: ErrorCode,
    max_stdout_bytes: int = 4 * 1024 * 1024,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    # Files prevent maliciously large tool output from filling the Python heap.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=output,
                stderr=errors, start_new_session=True,
            )
        except OSError as exc:
            raise MediaError(failure, f"{args[0]} could not be started", details={"reason": str(exc)}) from exc
        try:
            if should_cancel is None:
                process.wait(timeout=timeout)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    if should_cancel():
                        raise MediaError(ErrorCode.CANCELLED, "Media processing was cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaError(ErrorCode.PROCESSING_TIMEOUT, f"{args[0]} timed out")
                    try:
                        process.wait(timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
        except subprocess.TimeoutExpired as exc:
            _terminate(process)
            raise MediaError(ErrorCode.PROCESSING_TIMEOUT, f"{args[0]} timed out") from exc
        except BaseException:
            _terminate(process)
            raise
        output.seek(0)
        stdout_bytes = output.read(max_stdout_bytes + 1)
        if len(stdout_bytes) > max_stdout_bytes:
            raise MediaError(failure, f"{args[0]} returned too much output")
        errors.seek(0, os.SEEK_END)
        errors.seek(max(0, errors.tell() - 2000))
        stderr = errors.read().decode("utf-8", errors="replace")
        stdout = stdout_bytes.decode("utf-8", errors="replace")
    if process.returncode != 0:
        detail = stderr.strip()[-2000:]
        if "No space left on device" in detail:
            code = ErrorCode.INSUFFICIENT_DISK_SPACE
        elif any(
            phrase in detail for phrase in (
                "Unknown encoder", "Encoder not found", "Unknown decoder",
                "Decoder not found", "Unsupported codec",
            )
        ):
            code = ErrorCode.UNSUPPORTED_CODEC
        else:
            code = failure
        raise MediaError(code, f"{args[0]} failed", details={"returncode": process.returncode, "stderr": detail})
    return stdout, stderr


def _terminate(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # A child in the process group may outlive FFmpeg's parent process.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _progress_seconds(line: bytes) -> float | None:
    key, separator, value = line.strip().partition(b"=")
    if not separator:
        return None
    try:
        if key in (b"out_time_us", b"out_time_ms"):
            return max(0.0, int(value) / 1_000_000)
        if key == b"out_time":
            hours, minutes, seconds = value.split(b":")
            timestamp = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            return max(0.0, timestamp) if math.isfinite(timestamp) else None
    except (ValueError, OverflowError):
        pass
    return None


def run_progress_command(
    args: list[str], *, timeout: float,
    on_progress: Callable[[float], None],
    should_cancel: Callable[[], bool],
) -> None:
    """Consume FFmpeg's progress stream while enforcing time and cancellation limits."""
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=errors, start_new_session=True,
            )
        except OSError as exc:
            raise MediaError(ErrorCode.FFMPEG_FAILED, "FFmpeg could not be started") from exc
        deadline = time.monotonic() + timeout
        try:
            assert process.stdout is not None
            descriptor = process.stdout.fileno()
            os.set_blocking(descriptor, False)
            pending = b""
            discard_line = False
            while True:
                if should_cancel():
                    raise MediaError(ErrorCode.CANCELLED, "Processing was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "FFmpeg timed out")
                ready, _, _ = select.select([descriptor], [], [], min(0.25, remaining))
                if not ready:
                    continue
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                for part in chunk.split(b"\n")[:-1]:
                    line = pending + part
                    pending = b""
                    if not discard_line:
                        seconds = _progress_seconds(line)
                        if seconds is not None:
                            on_progress(seconds)
                    discard_line = False
                tail = chunk.rsplit(b"\n", 1)[-1]
                if len(chunk.split(b"\n")) == 1:
                    pending += tail
                else:
                    pending = tail
                if len(pending) > 65536:
                    pending = b""
                    discard_line = True
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            _terminate(process)
            raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "FFmpeg timed out") from exc
        except BaseException:
            _terminate(process)
            raise
        finally:
            if process.stdout:
                process.stdout.close()
        if process.returncode != 0:
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 2000))
            detail = errors.read().decode("utf-8", errors="replace").strip()[-2000:]
            code = ErrorCode.INSUFFICIENT_DISK_SPACE if "No space left on device" in detail else ErrorCode.FFMPEG_FAILED
            raise MediaError(code, "FFmpeg failed", details={"returncode": process.returncode, "stderr": detail})
