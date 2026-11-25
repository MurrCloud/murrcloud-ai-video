"""Quota-locked publication of generated artifacts, with optional private S3 archive."""

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.storage import StorageError, _storage_root, _usage

EXTENSIONS = {"mp4", "mp3", "wav", "srt"}


@dataclass(frozen=True)
class StoredArtifact:
    storage_key: str
    size_bytes: int
    sha256: str


def artifact_key(project_id, asset_id, extension):
    if extension not in EXTENSIONS:
        raise StorageError("invalid_artifact_extension")
    return f"{UUID(str(project_id))}/productions/{UUID(str(asset_id))}.{extension}"


def artifact_path(settings, key, *, must_exist=True):
    try:
        project, category, filename = key.split("/")
        stem, extension = filename.rsplit(".", 1)
        if key != artifact_key(project, stem, extension) or category != "productions":
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise StorageError("invalid_artifact_key") from None
    root = _storage_root(settings)
    path = root / key
    if path.resolve() != path or (must_exist and not path.is_file()):
        raise StorageError("artifact_not_found")
    return path


def digest_file(path, max_bytes):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise StorageError("unsafe_artifact")
    size, digest = 0, hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise StorageError("artifact_too_large")
            digest.update(chunk)
    if not size:
        raise StorageError("empty_artifact")
    return size, digest.hexdigest()


def publish_file(settings, source, project_id, asset_id, extension, max_bytes):
    source = Path(source)
    size, digest = digest_file(source, max_bytes)
    key = artifact_key(project_id, asset_id, extension)
    final = artifact_path(settings, key, must_exist=False)
    for folder in (final.parent.parent, final.parent):
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.resolve() != folder:
            raise StorageError("unsafe_artifact")
    root = _storage_root(settings)
    descriptor = os.open(root / ".quota.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StorageError("unsafe_storage_entry")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StorageError("storage_busy", retryable=True) from None
        if final.exists():
            if digest_file(final, max_bytes) != (size, digest):
                raise StorageError("artifact_publication_conflict")
            return StoredArtifact(key, size, digest)
        disk = os.statvfs(root)
        if (
            _usage(root) + size > settings.storage_max_bytes
            or disk.f_bavail * disk.f_frsize < size + settings.storage_min_free_bytes
        ):
            raise StorageError("storage_quota_exceeded")
        partial = final.with_suffix(".part")
        if partial.exists():
            if partial.is_symlink() or not partial.is_file():
                raise StorageError("unsafe_artifact")
            partial.unlink()
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with source.open("rb") as incoming, os.fdopen(fd, "wb") as target:
                copied, checksum = 0, hashlib.sha256()
                while chunk := incoming.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > size:
                        raise StorageError("artifact_changed")
                    target.write(chunk)
                    checksum.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            if copied != size or checksum.hexdigest() != digest:
                raise StorageError("artifact_changed")
            os.link(partial, final, follow_symlinks=False)
        finally:
            if partial.exists() and not partial.is_dir():
                partial.unlink()
    return StoredArtifact(key, size, digest)


class S3Archive:
    """Private archive; hot local copies stay quota-bound and owner-scoped."""

    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client

    def archive(self, path, key, mime_type, sha256, size_bytes):
        if not self.settings.s3_enabled:
            return {"status": "disabled"}
        try:
            if self.client is None:
                request = {
                    "parent_pid": os.getpid(),
                    "path": str(Path(path).absolute()),
                    "key": key,
                    "mime_type": mime_type,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "storage_dir": str(self.settings.storage_dir),
                    "max_media_bytes": self.settings.max_media_bytes,
                    "endpoint": self.settings.s3_endpoint_url,
                    "bucket": self.settings.s3_bucket,
                    "region": self.settings.s3_region,
                    "access_key": self.settings.s3_access_key_id.get_secret_value(),
                    "secret_key": self.settings.s3_secret_access_key.get_secret_value(),
                }
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "app.archive_worker"],
                        input=json.dumps(request).encode(),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=90,
                        check=False,
                        start_new_session=True,
                        env={
                            "PATH": os.defpath,
                            "PYTHONDONTWRITEBYTECODE": "1",
                            "OMP_NUM_THREADS": "1",
                        },
                    )
                except subprocess.TimeoutExpired:
                    raise StorageError("archive_timeout", retryable=True) from None
                reply = json.loads(result.stdout) if len(result.stdout) <= 1024 else {}
                if (
                    result.returncode != 0
                    or reply.get("status") != "archived"
                    or reply.get("sha256") != sha256
                ):
                    raise StorageError("archive_unavailable", retryable=True)
                return {"status": "archived", "sha256": sha256}
            # The key is generated by this application, never an external URL.
            artifact_path(self.settings, key)
            if digest_file(Path(path), self.settings.max_media_bytes) != (size_bytes, sha256):
                raise StorageError("archive_source_changed")
            from botocore.exceptions import ClientError

            try:
                with Path(path).open("rb") as body:
                    self.client.put_object(
                        Bucket=self.settings.s3_bucket,
                        Key=key,
                        Body=body,
                        ContentLength=size_bytes,
                        ContentType=mime_type,
                        Metadata={"sha256": sha256},
                        IfNoneMatch="*",
                        CacheControl="private, no-store",
                    )
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) not in {
                    "PreconditionFailed",
                    "412",
                }:
                    raise
                existing = self.client.head_object(Bucket=self.settings.s3_bucket, Key=key)
                if (
                    existing.get("ContentLength") != size_bytes
                    or existing.get("Metadata", {}).get("sha256") != sha256
                ):
                    raise StorageError("archive_object_conflict") from None
            return {"status": "archived", "sha256": sha256}
        except StorageError:
            raise
        except Exception:
            raise StorageError("archive_unavailable", retryable=True) from None
