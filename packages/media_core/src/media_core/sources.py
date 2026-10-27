"""Authorized local and HTTPS media acquisition.

URL connections are pinned to a validated public DNS answer. TLS still checks
the original hostname, and each redirect is validated independently.
"""

from __future__ import annotations

import errno
import http.client
import ipaddress
import os
import socket
import stat
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urljoin, urlsplit

from .errors import ErrorCode, MediaError
from .models import MediaInfo
from .names import safe_stem


_DNS_LOOKUPS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="media-dns")


@dataclass(frozen=True)
class StagedMedia:
    path: Path
    filename: str


class SourceAdapter(Protocol):
    def validate(self, reference: str) -> None:
        """Reject an unsupported or unauthorized source."""

    def stage(self, reference: str, destination: Path) -> StagedMedia:
        """Copy a bounded source into a private directory."""

    def inspect(self, reference: str, destination: Path) -> MediaInfo:
        """Stage and inspect source streams."""


def _safe_filename(name: str) -> str:
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(leaf).suffix.lower()
    if len(suffix) > 11 or not suffix[1:].isalnum():
        suffix = ".bin"
    return safe_stem(leaf) + suffix


class _InspectableSource:
    def inspect(self, reference: str, destination: Path) -> MediaInfo:
        from .probe import inspect_file

        return inspect_file(self.stage(reference, destination).path)


class LocalFileSource(_InspectableSource):
    """Read uploads only from a trusted staging root, never arbitrary host paths."""

    def __init__(self, allowed_root: Path, *, max_bytes: int = 512 * 1024 * 1024):
        self.allowed_root = allowed_root.resolve()
        self.max_bytes = max_bytes

    def _path(self, reference: str) -> Path:
        if not reference:
            raise MediaError(ErrorCode.INVALID_SOURCE, "A local media path is required")
        candidate = Path(reference)
        if not candidate.is_absolute():
            candidate = self.allowed_root / candidate
        try:
            path = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MediaError(ErrorCode.MEDIA_NOT_FOUND, "Local media was not found") from exc
        if not path.is_relative_to(self.allowed_root):
            raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "Local media is outside the allowed root")
        try:
            info = path.stat()
        except OSError as exc:
            raise MediaError(ErrorCode.MEDIA_NOT_FOUND, "Local media was not found") from exc
        if not stat.S_ISREG(info.st_mode):
            raise MediaError(ErrorCode.INVALID_SOURCE, "Local media must be a regular file")
        if info.st_size > self.max_bytes:
            raise MediaError(ErrorCode.SIZE_LIMIT_EXCEEDED, "Local media exceeds the input size limit")
        return path

    def validate(self, reference: str) -> None:
        self._path(reference)

    def stage(self, reference: str, destination: Path) -> StagedMedia:
        path = self._path(reference)
        try:
            return self._stage_path(path, destination)
        except OSError as exc:
            raise _storage_error(exc) from exc

    def _stage_path(self, path: Path, destination: Path) -> StagedMedia:
        destination.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(path.name)
        target = destination / filename
        with tempfile.NamedTemporaryFile(dir=destination, prefix=".incoming-", delete=False) as temp:
            temp_path = Path(temp.name)
            try:
                size = 0
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise MediaError(ErrorCode.SIZE_LIMIT_EXCEEDED, "Local media exceeds the input size limit")
                        temp.write(chunk)
                temp.flush()
                os.fsync(temp.fileno())
                os.replace(temp_path, target)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
        return StagedMedia(target, filename)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: tuple, timeout: float):
        super().__init__(host, port=443, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        family, socktype, proto, _, sockaddr = self._address
        raw = socket.socket(family, socktype, proto)
        try:
            raw.settimeout(self.timeout)
            raw.connect(sockaddr)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class DirectURLSource(_InspectableSource):
    """Download only exact allowlisted HTTPS hosts on port 443."""

    def __init__(
        self,
        allowed_hosts: set[str] | frozenset[str],
        *,
        max_bytes: int = 512 * 1024 * 1024,
        timeout_seconds: float = 60,
        connect_timeout_seconds: float = 10,
        max_redirects: int = 3,
    ):
        self.allowed_hosts = frozenset(host.lower().rstrip(".") for host in allowed_hosts)
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.max_redirects = max_redirects

    def _validated(self, reference: str, deadline: float | None = None) -> tuple[str, str, tuple]:
        try:
            parsed = urlsplit(reference)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise MediaError(ErrorCode.INVALID_SOURCE, "Malformed media URL") from exc
        if parsed.scheme.lower() != "https" or not host or port not in (None, 443):
            raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "Only HTTPS URLs on port 443 are supported")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "URL credentials and fragments are not allowed")
        host = host.lower().rstrip(".")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "IP literal URLs are not allowed")
        if host not in self.allowed_hosts:
            raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "URL host is not allowlisted")
        remaining = self.connect_timeout_seconds
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        if remaining <= 0:
            raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "Media URL validation timed out")
        lookup = _DNS_LOOKUPS.submit(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
        try:
            answers = lookup.result(timeout=remaining)
        except FutureTimeout as exc:
            lookup.cancel()
            raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "Media URL DNS lookup timed out") from exc
        except OSError as exc:
            raise MediaError(ErrorCode.INVALID_SOURCE, "URL hostname could not be resolved") from exc
        if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
            raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "URL resolves to a blocked network address")
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        return host, target, answers[0]

    def validate(self, reference: str) -> None:
        self._validated(reference)

    def stage(self, reference: str, destination: Path) -> StagedMedia:
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _storage_error(exc) from exc
        deadline = time.monotonic() + self.timeout_seconds
        current = reference
        for redirect in range(self.max_redirects + 1):
            host, target, address = self._validated(current, deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "Media download timed out")
            conn = _PinnedHTTPSConnection(host, address, min(self.connect_timeout_seconds, remaining))
            try:
                conn.request("GET", target, headers={"Accept": "video/*, audio/*, application/octet-stream"})
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location or redirect == self.max_redirects:
                        raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "Media URL redirected too many times")
                    current = urljoin(current, location)
                    continue
                if response.status == 404:
                    raise MediaError(ErrorCode.MEDIA_NOT_FOUND, "Remote media was not found")
                if response.status != 200:
                    raise MediaError(ErrorCode.INVALID_SOURCE, f"Remote media returned HTTP {response.status}")
                length = response.getheader("Content-Length")
                if length and length.isdigit() and int(length) > self.max_bytes:
                    raise MediaError(ErrorCode.SIZE_LIMIT_EXCEEDED, "Remote media exceeds the input size limit")
                filename = _safe_filename(unquote(urlsplit(current).path.rsplit("/", 1)[-1]) or "media.bin")
                target_path = destination / filename
                with tempfile.NamedTemporaryFile(dir=destination, prefix=".incoming-", delete=False) as temp:
                    temp_path = Path(temp.name)
                    try:
                        size = 0
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "Media download timed out")
                            if conn.sock:
                                conn.sock.settimeout(min(self.connect_timeout_seconds, remaining))
                            chunk = response.read(min(64 * 1024, self.max_bytes - size + 1))
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise MediaError(ErrorCode.SIZE_LIMIT_EXCEEDED, "Remote media exceeds the input size limit")
                            temp.write(chunk)
                        temp.flush()
                        os.fsync(temp.fileno())
                        os.replace(temp_path, target_path)
                    except BaseException:
                        temp_path.unlink(missing_ok=True)
                        raise
                return StagedMedia(target_path, filename)
            except TimeoutError as exc:
                raise MediaError(ErrorCode.PROCESSING_TIMEOUT, "Media download timed out") from exc
            except OSError as exc:
                if exc.errno in (errno.ENOSPC, errno.EACCES, errno.EROFS):
                    raise _storage_error(exc) from exc
                raise MediaError(ErrorCode.INVALID_SOURCE, "Remote media download failed") from exc
            except http.client.HTTPException as exc:
                raise MediaError(ErrorCode.INVALID_SOURCE, "Remote media download failed") from exc
            finally:
                conn.close()
        raise MediaError(ErrorCode.UNAUTHORIZED_SOURCE, "Media URL redirected too many times")


def _storage_error(exc: OSError) -> MediaError:
    code = ErrorCode.INSUFFICIENT_DISK_SPACE if exc.errno == errno.ENOSPC else ErrorCode.INTERNAL_ERROR
    return MediaError(code, "Media staging failed", details={"reason": str(exc)})

