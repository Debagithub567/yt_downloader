"""Deterministic platform URL and extractor-metadata boundary checks."""

import pytest
from dataclasses import replace

from media_core.errors import ErrorCode, MediaError
from media_core.platforms import (Platform, PlatformInspection, SourceKind, classify_url,
                                  normalize_metadata, platform_snapshot, source_from_snapshot)


@pytest.mark.parametrize(("url", "platform", "canonical"), [
    ("https://www.youtube.com/watch?v=AbCdEf12345&t=42", Platform.YOUTUBE,
     "https://www.youtube.com/watch?v=AbCdEf12345"),
    ("https://youtu.be/AbCdEf12345?si=tracking", Platform.YOUTUBE,
     "https://www.youtube.com/watch?v=AbCdEf12345"),
    ("https://m.youtube.com/shorts/AbCdEf12345", Platform.YOUTUBE,
     "https://www.youtube.com/watch?v=AbCdEf12345"),
    ("https://www.instagram.com/reel/Cx12345ab/?igsh=tracking", Platform.INSTAGRAM,
     "https://www.instagram.com/reel/Cx12345ab/"),
    ("https://x.com/some_user/status/1234567890123456789?s=20", Platform.X,
     "https://x.com/some_user/status/1234567890123456789"),
    ("https://twitter.com/some_user/status/1234567890123456789", Platform.X,
     "https://x.com/some_user/status/1234567890123456789"),
])
def test_classifies_supported_platform_paths(url, platform, canonical):
    result = classify_url(url)
    assert result.kind == SourceKind.PLATFORM
    assert result.platform == platform
    assert result.url == canonical
    assert "tracking" not in result.url


def test_unknown_https_host_remains_direct_media():
    result = classify_url("https://media.example/video.mp4")
    assert result.kind == SourceKind.DIRECT_URL
    assert result.platform is None


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.example/watch?v=AbCdEf12345",
    "https://notyoutube.com/watch?v=AbCdEf12345",
    "https://youtube.com/playlist?list=123",
    "https://youtu.be/not-an-id",
    "https://instagram.com/accounts/login/",
    "https://x.com/user/status/not-a-number",
    "https://youtube.com/watch?v=AbCdEf12345&v=AAAAAAAAAAA",
    "https://youtube.com/shorts/%2e%2e/AbCdEf12345",
    "https://youtube.com//watch?v=AbCdEf12345",
    "https://youtube.com./watch?v=AbCdEf12345",
    "https://youtube.com@evil.example/watch?v=AbCdEf12345",
    "https://user:pass@youtube.com/watch?v=AbCdEf12345",
    "http://youtube.com/watch?v=AbCdEf12345",
    "file:///etc/passwd",
    "https://youtube.com:444/watch?v=AbCdEf12345",
    "https://youtube.com/watch?v=AbCdEf12345#fragment",
    "https://youtube.com/watch?v=AbCdEf12345\nAuthorization: Bearer secret",
    "https://youtube.com:bad/watch?v=AbCdEf12345",
])
def test_rejects_invalid_or_suspicious_urls(url):
    result = None
    try:
        result = classify_url(url)
    except MediaError:
        pass
    assert result is None or result.kind == SourceKind.DIRECT_URL


def test_normalizes_only_bounded_public_metadata():
    info = {
        "title": "A unsafe; title", "duration": 12.5, "availability": "public",
        "webpage_url": "https://secret.example/?token=private",
        "http_headers": {"Authorization": "Bearer private"},
        "formats": [
            {"format_id": "h264-720", "ext": "mp4", "vcodec": "h264", "acodec": "none",
             "width": 1280, "height": 720, "filesize": 12345,
             "url": "https://secret.example/video?token=private"},
            {"format_id": "aac", "ext": "m4a", "vcodec": "none", "acodec": "aac",
             "filesize_approx": 2345},
            {"format_id": "../unsafe", "vcodec": "h264"},
        ],
    }
    result = normalize_metadata(Platform.YOUTUBE, info)
    assert result.title == "A unsafe_ title"
    assert result.duration_seconds == 12.5
    assert result.available_resolutions == (720,)
    assert result.has_audio
    assert len(result.formats) == 2
    assert result.formats[0].size_bytes == 12345
    assert not result.formats[0].size_is_estimate
    assert result.formats[1].size_is_estimate
    assert "private" not in repr(result)


def test_missing_metadata_is_explicitly_unknown():
    result = normalize_metadata(Platform.X, {"formats": []})
    assert result.title is None
    assert result.duration_seconds is None
    assert result.formats == ()
    assert result.available_resolutions == ()
    assert result.has_audio is False


@pytest.mark.parametrize("info,code", [
    ({"_type": "playlist", "entries": []}, ErrorCode.INVALID_MEDIA),
    ({"availability": "needs_auth"}, ErrorCode.UNAUTHORIZED_SOURCE),
    ({"is_live": True}, ErrorCode.UNAUTHORIZED_SOURCE),
])
def test_rejects_nonpublic_or_nonvideo_metadata(info, code):
    with pytest.raises(MediaError) as error:
        normalize_metadata(Platform.YOUTUBE, info)
    assert error.value.code == code


def test_inspection_snapshot_rejects_forged_metadata_and_identity():
    source = classify_url("https://youtu.be/AbCdEf12345")
    metadata = normalize_metadata(Platform.YOUTUBE, {"title": "Safe", "formats": [
        {"format_id": "a1", "vcodec": "none", "acodec": "aac"}]})
    snapshot = platform_snapshot(PlatformInspection(source, metadata))
    assert source_from_snapshot(snapshot) == source
    with pytest.raises(MediaError):
        platform_snapshot(PlatformInspection(source, replace(metadata, title="x" * 161)))
    with pytest.raises(MediaError):
        platform_snapshot(PlatformInspection(source, replace(metadata, platform=Platform.X)))
    with pytest.raises(MediaError):
        source_from_snapshot({**snapshot, "url": "https://evil.example/private"})
