import asyncio
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app import storage

MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 52


@pytest.fixture
def storage_settings(tmp_path):
    return SimpleNamespace(
        storage_dir=tmp_path.resolve() / "clips",
        max_media_bytes=1024,
        storage_max_bytes=2048,
        storage_min_free_bytes=1024**2,
        download_timeout_seconds=30,
    )


class MockResponse(io.BytesIO):
    def __init__(self, body=MP4, *, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": "video/mp4", "Content-Length": str(len(body))}
        self.headers.update(headers or {})

    def getheader(self, key, default=None):
        return self.headers.get(key, default)


@pytest.fixture
def transport(monkeypatch):
    captured = {}
    response = MockResponse()

    class Connection:
        def __init__(self, host, address):
            captured.update(host=host, address=address)

        def request(self, method, path, headers):
            captured.update(method=method, path=path, headers=headers)

        def getresponse(self):
            return captured.get("response", response)

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(storage, "resolve_public_address", lambda host: "93.184.216.34")
    monkeypatch.setattr(storage, "PinnedHTTPSConnection", Connection)
    return captured


@pytest.mark.parametrize(
    "url",
    [
        "http://v3.fal.media/x.mp4",
        "https://127.0.0.1/x",
        "https://169.254.169.254/x",
        "https://v3.fal.media.evil.test/x",
        "https://fal.media@localhost/x",
        "https://user:secret@v3.fal.media/x",
        "https://v3.fal.media:8443/x",
        "https://v3.fal.media/x#secret",
        "file:///etc/passwd",
        "https://v3.fal.media/x\r\nsecret",
    ],
)
def test_unapproved_media_urls_are_rejected_without_fetch(url):
    with pytest.raises(storage.StorageError, match="unsafe_media_url"):
        storage.validate_media_url(url)


def test_dns_mixed_public_private_addresses_are_rejected(monkeypatch):
    monkeypatch.setattr(
        storage.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=json.dumps(["93.184.216.34", "127.0.0.1"])),
    )
    with pytest.raises(storage.StorageError, match="private_media_address"):
        storage.resolve_public_address("v3.fal.media")


def test_dns_is_bounded_and_does_not_inherit_provider_secrets(monkeypatch):
    captured = {}

    def resolver(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(stdout='["93.184.216.34"]')

    monkeypatch.setattr(storage.subprocess, "run", resolver)
    assert storage.resolve_public_address("v3.fal.media") == "93.184.216.34"
    assert captured["timeout"] == 5
    assert "VIDEO_FAL_KEY" not in captured["env"]


def test_download_is_bounded_private_atomic_and_idempotent(storage_settings, transport):
    project, job = uuid4(), uuid4()
    clip = asyncio.run(
        storage.download_provider_video(
            "https://v3.fal.media/files/result.mp4?token=private", project, job, storage_settings
        )
    )
    path = storage.local_asset_path(storage_settings, clip.storage_key)
    assert path.read_bytes() == MP4
    assert path.stat().st_mode & 0o077 == 0
    assert clip.size_bytes == len(MP4) and len(clip.sha256) == 64
    assert transport["address"] == "93.184.216.34"
    assert "Authorization" not in transport["headers"]
    assert not list(Path(storage_settings.storage_dir).rglob("*.part"))
    transport["response"] = MockResponse(b"would be invalid")
    repeated = asyncio.run(
        storage.download_provider_video(
            "https://v3.fal.media/files/result.mp4", project, job, storage_settings
        )
    )
    assert repeated == clip


@pytest.mark.parametrize(
    "response,code",
    [
        (MockResponse(status=302), "media_http_error"),
        (MockResponse(headers={"Content-Type": "text/html"}), "invalid_media_type"),
        (MockResponse(headers={"Content-Encoding": "gzip"}), "encoded_media_rejected"),
        (MockResponse(headers={"Content-Length": "99999"}), "media_too_large"),
        (MockResponse(headers={"Content-Length": "nonsense"}), "invalid_media_length"),
        (MockResponse(b"x" * 64), "invalid_mp4_signature"),
        (MockResponse(headers={"Content-Length": "100"}), "incomplete_media"),
    ],
)
def test_bad_downloads_leave_no_artifact(storage_settings, transport, response, code):
    transport["response"] = response
    with pytest.raises(storage.StorageError, match=code):
        asyncio.run(
            storage.download_provider_video(
                "https://v3.fal.media/x.mp4", uuid4(), uuid4(), storage_settings
            )
        )
    assert not list(Path(storage_settings.storage_dir).rglob("*.mp4"))
    assert not list(Path(storage_settings.storage_dir).rglob("*.part"))
    assert transport["closed"]


def test_media_byte_cap_survives_missing_content_length(storage_settings, transport):
    response = MockResponse(MP4 + b"x" * 2048)
    del response.headers["Content-Length"]
    transport["response"] = response
    with pytest.raises(storage.StorageError, match="media_too_large"):
        asyncio.run(
            storage.download_provider_video(
                "https://v3.fal.media/x.mp4", uuid4(), uuid4(), storage_settings
            )
        )
    assert not list(Path(storage_settings.storage_dir).rglob("*.mp4"))


def test_storage_quota_and_traversal_are_enforced(storage_settings, transport):
    storage_settings.storage_max_bytes = 32
    with pytest.raises(storage.StorageError, match="storage_quota_exceeded"):
        asyncio.run(
            storage.download_provider_video(
                "https://v3.fal.media/x.mp4", uuid4(), uuid4(), storage_settings
            )
        )
    for key in ("../etc/passwd", "/etc/passwd", f"{uuid4()}/../x.mp4"):
        with pytest.raises(storage.StorageError, match="invalid_storage_key"):
            storage.local_asset_path(storage_settings, key)


@pytest.mark.parametrize("phase", ["headers", "body", "detached_body"])
def test_absolute_deadline_interrupts_stalled_header_or_body_reads(
    storage_settings, monkeypatch, phase
):
    released = threading.Event()

    class Socket:
        def shutdown(self, how):
            released.set()

        def close(self):
            released.set()

    class Response(MockResponse):
        def read(self, amount):
            assert released.wait(2), "Absolute deadline did not interrupt the network"
            raise OSError("simulated network shutdown")

    class Connection:
        sock = Socket()
        network_socket = sock

        def __init__(self, host, address):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            if phase == "headers":
                assert released.wait(2), "Absolute deadline did not interrupt headers"
                raise OSError("simulated network shutdown")
            if phase == "detached_body":
                self.sock = None
            return Response()

        def close(self):
            self.network_socket.close()

    monkeypatch.setattr(storage, "resolve_public_address", lambda host: "93.184.216.34")
    monkeypatch.setattr(storage, "PinnedHTTPSConnection", Connection)
    storage_settings.download_timeout_seconds = 0.05
    began = time.monotonic()
    with pytest.raises(storage.StorageError, match="media_timeout"):
        asyncio.run(
            storage.download_provider_video(
                "https://v3.fal.media/x.mp4", uuid4(), uuid4(), storage_settings
            )
        )
    assert time.monotonic() - began < 1
    assert not list(Path(storage_settings.storage_dir).rglob("*.part"))
    assert not list(Path(storage_settings.storage_dir).rglob("*.mp4"))
