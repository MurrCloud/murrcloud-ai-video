"""Bounded provider-only MP4 downloads with pinned public DNS and authenticated local access."""

import asyncio
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


class StorageError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class StoredClip:
    storage_key: str
    size_bytes: int
    sha256: str
    mime_type: str = "video/mp4"


def validate_media_url(url: str):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or len(url) > 8192
            or any(ord(c) < 32 for c in url)
            or not any(host == suffix or host.endswith("." + suffix) for suffix in ("fal.media",))
        ):
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise StorageError("unsafe_media_url") from None


def resolve_public_address(host: str) -> str:
    # A short-lived resolver subprocess gives getaddrinfo a real wall-clock bound.
    # It receives only a validated CDN hostname, and none of the provider secrets.
    program = "import json,socket,sys; print(json.dumps(list({a[4][0] for a in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)})))"
    try:
        result = subprocess.run(
            [sys.executable, "-c", program, host],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        addresses = json.loads(result.stdout)
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= 32:
            raise ValueError
        for address in addresses:
            if not ipaddress.ip_address(address).is_global:
                raise StorageError("private_media_address")
        return addresses[0]
    except StorageError:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise StorageError("media_dns_unavailable", retryable=True) from None


class PinnedHTTPSConnection(http.client.HTTPConnection):
    default_port = 443

    def __init__(self, host: str, address: str):
        super().__init__(host, 443, timeout=10)
        self.address = address
        self.deadline = None
        self.network_socket = None
        self.abort_event = None
        self.tls_context = ssl.create_default_context()
        self.tls_context.set_alpn_protocols(["http/1.1"])

    def connect(self):
        # Connect to the IP we checked, preserving hostname verification and SNI.
        remaining = self.timeout if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        raw = socket.create_connection((self.address, 443), timeout=min(self.timeout, remaining))
        self.sock = raw
        self.network_socket = raw
        try:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise TimeoutError
            # Publish the TLS socket before handshaking, so the absolute-deadline
            # watchdog can interrupt a peer dripping handshake/header/body bytes.
            self.sock = self.tls_context.wrap_socket(
                raw, server_hostname=self.host, do_handshake_on_connect=False
            )
            self.network_socket = self.sock
            if (self.abort_event is not None and self.abort_event.is_set()) or (
                self.deadline is not None and time.monotonic() >= self.deadline
            ):
                raise TimeoutError
            self.sock.do_handshake()
        except BaseException:
            self.close()
            raw.close()
            raise


def _storage_root(settings) -> Path:
    root = Path(settings.storage_dir).absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.resolve() != root or not root.is_dir():
        raise StorageError("unsafe_storage_root")
    return root


def local_asset_path(settings, storage_key: str) -> Path:
    try:
        parts = storage_key.split("/")
        if len(parts) != 2 or str(UUID(parts[0])) != parts[0]:
            raise ValueError
        stem = parts[1].removesuffix(".mp4")
        if parts[1] != str(UUID(stem)) + ".mp4":
            raise ValueError
    except (ValueError, AttributeError):
        raise StorageError("invalid_storage_key") from None
    root = _storage_root(settings)
    path = root / storage_key
    if path.resolve() != path or not path.is_file():
        raise StorageError("asset_not_found")
    return path


def _usage(root: Path) -> int:
    total = 0
    for directory, subdirs, names in os.walk(root, followlinks=False):
        for name in [*subdirs, *names]:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise StorageError("unsafe_storage_entry")
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _digest_existing(path: Path, key: str, max_bytes: int) -> StoredClip:
    size, digest, prefix = 0, hashlib.sha256(), b""
    if path.is_symlink():
        raise StorageError("unsafe_storage_entry")
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise StorageError("media_too_large")
            digest.update(chunk)
            if len(prefix) < 16:
                prefix += chunk[:16]
    _verify_mp4(prefix, size)
    return StoredClip(key, size, digest.hexdigest())


def _verify_mp4(prefix: bytes, size: int):
    if (
        size < 16
        or prefix[4:8] != b"ftyp"
        or prefix[8:12]
        not in {
            b"isom",
            b"iso2",
            b"iso4",
            b"iso5",
            b"iso6",
            b"mp41",
            b"mp42",
            b"avc1",
            b"dash",
            b"M4V ",
        }
    ):
        raise StorageError("invalid_mp4_signature")


def _download(url: str, project_id: UUID, job_id: UUID, settings) -> StoredClip:
    parsed = validate_media_url(url)
    root = _storage_root(settings)
    key = f"{UUID(str(project_id))}/{UUID(str(job_id))}.mp4"
    folder = root / str(UUID(str(project_id)))
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.resolve() != folder:
        raise StorageError("unsafe_storage_entry")
    final, partial = root / key, folder / (str(job_id) + ".part")
    lock_path = root / ".quota.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise StorageError("unsafe_storage_entry")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StorageError("storage_busy", retryable=True) from None
        if final.exists():
            return _digest_existing(final, key, settings.max_media_bytes)
        if partial.exists() or partial.is_symlink():
            partial.unlink()  # exact server-generated job path; no recursive deletion
        usage = _usage(root)
        if usage >= settings.storage_max_bytes:
            raise StorageError("storage_quota_exceeded")
        address = resolve_public_address(parsed.hostname)
        connection = PinnedHTTPSConnection(parsed.hostname, address)
        written, digest, prefix = 0, hashlib.sha256(), b""
        deadline = time.monotonic() + settings.download_timeout_seconds
        connection.deadline = deadline
        timed_out = threading.Event()
        connection.abort_event = timed_out
        response = None

        def abort_network():
            timed_out.set()
            # HTTPConnection detaches .sock for Connection:close responses while
            # HTTPResponse's file reference remains open. Keep the real socket.
            sock = getattr(connection, "network_socket", None) or getattr(connection, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        timer = threading.Timer(settings.download_timeout_seconds, abort_network)
        timer.daemon = True
        timer.start()
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request(
                "GET", target, headers={"Accept": "video/mp4", "Accept-Encoding": "identity"}
            )
            response = connection.getresponse()
            # Never follow a redirect or send the API's Authorization header to media hosts.
            if response.status != 200:
                raise StorageError(
                    "media_http_error", retryable=response.status in {429, 500, 502, 503, 504}
                )
            if (
                response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                != "video/mp4"
            ):
                raise StorageError("invalid_media_type")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise StorageError("encoded_media_rejected")
            length = response.getheader("Content-Length")
            expected = None
            if length is not None:
                try:
                    expected = int(length)
                except ValueError:
                    raise StorageError("invalid_media_length") from None
                if expected < 16 or expected > settings.max_media_bytes:
                    raise StorageError("media_too_large")
            file_descriptor = os.open(
                partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(file_descriptor, "wb") as destination:
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    disk = os.statvfs(root)
                    if timed_out.is_set() or time.monotonic() > deadline:
                        raise StorageError("media_timeout", retryable=True)
                    if written > settings.max_media_bytes:
                        raise StorageError("media_too_large")
                    if (
                        usage + written > settings.storage_max_bytes
                        or disk.f_bavail * disk.f_frsize
                        < settings.storage_min_free_bytes + len(chunk)
                    ):
                        raise StorageError("storage_quota_exceeded")
                    destination.write(chunk)
                    digest.update(chunk)
                    if len(prefix) < 16:
                        prefix += chunk[:16]
                destination.flush()
                os.fsync(destination.fileno())
            if timed_out.is_set() or time.monotonic() > deadline:
                raise StorageError("media_timeout", retryable=True)
            _verify_mp4(prefix, written)
            if expected is not None and written != expected:
                raise StorageError("incomplete_media", retryable=True)
            # Atomic, non-overwriting publication; repeated completion reuses the same artifact.
            os.link(partial, final, follow_symlinks=False)
            partial.unlink()
            return StoredClip(key, written, digest.hexdigest())
        except StorageError:
            raise
        except (OSError, http.client.HTTPException):
            raise StorageError(
                "media_timeout" if timed_out.is_set() else "media_download_failed", retryable=True
            ) from None
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            connection.close()
            retained = getattr(connection, "network_socket", None)
            if retained is not None:
                retained.close()
            if partial.exists() and not partial.is_dir():
                partial.unlink()


async def download_provider_video(url: str, project_id: UUID, job_id: UUID, settings) -> StoredClip:
    return await asyncio.to_thread(_download, url, project_id, job_id, settings)
