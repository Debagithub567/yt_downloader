import socket
import ssl
import subprocess
import shutil
import threading
import time

import pytest

from media_core.errors import ErrorCode, MediaError
from media_core.sources import DirectURLSource, LocalFileSource
import media_core.sources as sources


PUBLIC = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
PRIVATE = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))


@pytest.fixture(scope="module")
def tls_certificate(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL CLI is required for the controlled HTTPS test")
    root = tmp_path_factory.mktemp("media-tls")
    cert, key = root / "cert.pem", root / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=media.example", "-addext", "subjectAltName=DNS:media.example"],
        check=True, capture_output=True, timeout=30,
    )
    return cert, key


def _controlled_tls_download(monkeypatch, tmp_path, tls_certificate, hostname):
    cert, key = tls_certificate
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert), str(key))
    client_context = ssl.create_default_context(cafile=str(cert))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    local_port = listener.getsockname()[1]
    received = []

    def serve():
        try:
            conn, _ = listener.accept()
            with server_context.wrap_socket(conn, server_side=True) as secure:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += secure.recv(4096)
                received.append(request)
                secure.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nmedia")
        except ssl.SSLError:
            # A hostname mismatch causes the client to abort the handshake.
            pass
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    raw_socket = socket.socket
    requested_addresses = []

    class RedirectedSocket(raw_socket):
        def connect(self, address):
            requested_addresses.append(address)
            return super().connect(("127.0.0.1", local_port))

    monkeypatch.setattr(socket, "socket", RedirectedSocket)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [PUBLIC])
    monkeypatch.setattr(ssl, "_create_default_https_context", lambda **kw: client_context)
    try:
        staged = DirectURLSource({hostname}).stage(
            f"https://{hostname}/clip.mp4", tmp_path / "staged",
        )
        return staged, requested_addresses, received
    finally:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_authorized_url_download_uses_pinned_address_and_verified_tls(monkeypatch, tmp_path, tls_certificate):
    staged, addresses, received = _controlled_tls_download(
        monkeypatch, tmp_path, tls_certificate, "media.example",
    )
    assert addresses == [("8.8.8.8", 443)]
    assert staged.path.read_bytes() == b"media"
    assert received and received[0].startswith(b"GET /clip.mp4 HTTP/1.1\r\n")
    assert b"Host: media.example\r\n" in received[0]


def test_authorized_url_rejects_tls_hostname_mismatch(monkeypatch, tmp_path, tls_certificate):
    with pytest.raises(MediaError) as exc:
        _controlled_tls_download(monkeypatch, tmp_path, tls_certificate, "other.example")
    assert exc.value.code == ErrorCode.INVALID_SOURCE
    assert not list((tmp_path / "staged").iterdir())


def test_local_source_is_bounded_and_confined(tmp_path):
    allowed = tmp_path / "uploads"
    allowed.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    (allowed / "link.mp4").symlink_to(outside)
    source = LocalFileSource(allowed, max_bytes=4)
    with pytest.raises(MediaError) as exc:
        source.validate(str(allowed / "link.mp4"))
    assert exc.value.code == ErrorCode.UNAUTHORIZED_SOURCE
    (allowed / "large.mp4").write_bytes(b"12345")
    with pytest.raises(MediaError) as exc:
        source.stage("large.mp4", tmp_path / "staging")
    assert exc.value.code == ErrorCode.SIZE_LIMIT_EXCEEDED
    staging = tmp_path / "staging"
    assert not staging.exists() or not list(staging.iterdir())


@pytest.mark.parametrize(
    "url",
    [
        "http://media.example/file.mp4",
        "https://media.example:8443/file.mp4",
        "https://user:pass@media.example/file.mp4",
        "https://media.example/file.mp4#fragment",
        "https://127.0.0.1/file.mp4",
        "https://evil.example/file.mp4",
        "file:///etc/passwd",
    ],
)
def test_url_rejects_unsafe_references(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [PUBLIC])
    with pytest.raises(MediaError) as exc:
        DirectURLSource({"media.example"}).validate(url)
    assert exc.value.code in (ErrorCode.UNAUTHORIZED_SOURCE, ErrorCode.INVALID_SOURCE)


def test_url_rejects_private_or_mixed_dns(monkeypatch):
    source = DirectURLSource({"media.example"})
    for addresses in (
        [PRIVATE],
        [PUBLIC, PRIVATE],
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))],
    ):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: addresses)
        with pytest.raises(MediaError) as exc:
            source.validate("https://media.example/file.mp4")
        assert exc.value.code == ErrorCode.UNAUTHORIZED_SOURCE


def test_dns_is_rechecked_before_download(monkeypatch, tmp_path):
    answers = iter(([PUBLIC], [PRIVATE]))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: next(answers))
    source = DirectURLSource({"media.example"})
    source.validate("https://media.example/file.mp4")
    with pytest.raises(MediaError) as exc:
        source.stage("https://media.example/file.mp4", tmp_path)
    assert exc.value.code == ErrorCode.UNAUTHORIZED_SOURCE
    assert not list(tmp_path.iterdir())


def test_dns_lookup_obeys_timeout(monkeypatch):
    def slow_lookup(*args, **kwargs):
        time.sleep(0.1)
        return [PUBLIC]

    monkeypatch.setattr(socket, "getaddrinfo", slow_lookup)
    with pytest.raises(MediaError) as exc:
        DirectURLSource({"media.example"}, connect_timeout_seconds=0.01).validate(
            "https://media.example/file.mp4"
        )
    assert exc.value.code == ErrorCode.PROCESSING_TIMEOUT


class FakeResponse:
    def __init__(self, *, status=200, location=None, chunks=(), length=None):
        self.status = status
        self.location = location
        self.chunks = iter(chunks)
        self.length = length

    def getheader(self, name):
        return {"Location": self.location, "Content-Length": self.length}.get(name)

    def read(self, size):
        return next(self.chunks, b"")


def _connection(monkeypatch, response, seen):
    class FakeConnection:
        sock = None

        def __init__(self, host, address, timeout):
            seen.append((host, address, timeout))

        def request(self, method, target, headers):
            assert method == "GET"
            assert target.startswith("/")

        def getresponse(self):
            return response

        def close(self):
            pass

    monkeypatch.setattr(sources, "_PinnedHTTPSConnection", FakeConnection)


def test_url_uses_pinned_ip_and_bounded_staging(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [PUBLIC])
    seen = []
    _connection(monkeypatch, FakeResponse(chunks=[b"1234", b""]), seen)
    staged = DirectURLSource({"media.example"}, max_bytes=4).stage(
        "https://media.example/%2E%2E%2Fclip.mp4", tmp_path,
    )
    assert staged.path == tmp_path / "clip.mp4"
    assert staged.path.read_bytes() == b"1234"
    assert seen[0][0] == "media.example"
    assert seen[0][1] == PUBLIC


def test_url_redirect_is_revalidated(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [PUBLIC])
    _connection(
        monkeypatch,
        FakeResponse(status=302, location="http://127.0.0.1/private"),
        [],
    )
    with pytest.raises(MediaError) as exc:
        DirectURLSource({"media.example"}).stage("https://media.example/file.mp4", tmp_path)
    assert exc.value.code == ErrorCode.UNAUTHORIZED_SOURCE
    assert not list(tmp_path.iterdir())


def test_url_size_limit_removes_temporary_file(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [PUBLIC])
    _connection(monkeypatch, FakeResponse(chunks=[b"12345"]), [])
    with pytest.raises(MediaError) as exc:
        DirectURLSource({"media.example"}, max_bytes=4).stage(
            "https://media.example/file.mp4", tmp_path,
        )
    assert exc.value.code == ErrorCode.SIZE_LIMIT_EXCEEDED
    assert not list(tmp_path.iterdir())


def test_url_timeout_is_structured(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [PUBLIC])

    class TimeoutConnection:
        def __init__(self, *args):
            pass

        def request(self, *args, **kwargs):
            raise TimeoutError("timed out")

        def close(self):
            pass

    monkeypatch.setattr(sources, "_PinnedHTTPSConnection", TimeoutConnection)
    with pytest.raises(MediaError) as exc:
        DirectURLSource({"media.example"}).stage("https://media.example/file.mp4", tmp_path)
    assert exc.value.code == ErrorCode.PROCESSING_TIMEOUT
    assert not list(tmp_path.iterdir())
