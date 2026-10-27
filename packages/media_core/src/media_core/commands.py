"""Run media tools without a shell and terminate their process groups on failure."""

import os
import signal
import subprocess
import tempfile

from .errors import ErrorCode, MediaError


def run_command(
    args: list[str], *, timeout: float, failure: ErrorCode,
    max_stdout_bytes: int = 4 * 1024 * 1024,
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
            process.wait(timeout=timeout)
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
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
