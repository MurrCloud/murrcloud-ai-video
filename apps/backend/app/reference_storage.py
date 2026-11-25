"""Private canonical references share the clip quota and lock."""

import fcntl
import os
import stat
from pathlib import Path
from uuid import UUID

from app.ingestion import CanonicalImage
from app.storage import StorageError, _storage_root, _usage


def reference_path(settings, object_key: str) -> Path:
    parts = object_key.split("/")
    try:
        if len(parts) != 3 or parts[1] != "references" or str(UUID(parts[0])) != parts[0]:
            raise ValueError
        filename = parts[2]
        stem, extension = filename.rsplit(".", 1)
        if extension not in {"jpg", "png"} or str(UUID(stem)) != stem:
            raise ValueError
    except (ValueError, AttributeError):
        raise StorageError("invalid_reference_key") from None
    root = _storage_root(settings)
    path = root / object_key
    if path.resolve() != path or not path.is_file():
        raise StorageError("reference_not_found")
    return path


def store_reference(image: CanonicalImage, project_id: UUID, asset_id: UUID, settings) -> str:
    root = _storage_root(settings)
    project = root / str(UUID(str(project_id)))
    project.mkdir(exist_ok=True, mode=0o700)
    if project.resolve() != project:
        raise StorageError("unsafe_reference_path")
    folder = project / "references"
    folder.mkdir(exist_ok=True, mode=0o700)
    if folder.resolve() != folder:
        raise StorageError("unsafe_reference_path")
    key = f"{project.name}/references/{UUID(str(asset_id))}.{image.extension}"
    final = root / key
    temporary = folder / (str(asset_id) + ".part")
    descriptor = os.open(root / ".quota.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StorageError("unsafe_storage_entry")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StorageError("storage_busy", retryable=True) from None
        disk = os.statvfs(root)
        if (
            _usage(root) + len(image.data) > settings.storage_max_bytes
            or disk.f_bavail * disk.f_frsize < settings.storage_min_free_bytes + len(image.data)
        ):
            raise StorageError("storage_quota_exceeded")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as destination:
                destination.write(image.data)
                destination.flush()
                os.fsync(destination.fileno())
            os.link(temporary, final, follow_symlinks=False)
            return key
        finally:
            if temporary.exists():
                temporary.unlink()


def remove_uncommitted_reference(settings, object_key: str) -> bool:
    """Remove one canonical reference after a serialized DB absence check.

    The caller establishes absence using a fresh database transaction. Deletion
    holds the shared quota lock and never traverses symlinks or removes folders.
    """
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
        try:
            reference_path(settings, object_key)
        except StorageError as exc:
            if exc.code == "reference_not_found":
                candidate = root / object_key
                if candidate.exists() or any(
                    part.is_symlink()
                    for part in (candidate, candidate.parent, candidate.parent.parent)
                ):
                    raise StorageError("unsafe_storage_entry") from None
                return False
            raise
        project, _, filename = object_key.split("/")
        descriptors = []
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_fd = os.open(root, flags)
            descriptors.append(root_fd)
            project_fd = os.open(project, flags, dir_fd=root_fd)
            descriptors.append(project_fd)
            references_fd = os.open("references", flags, dir_fd=project_fd)
            descriptors.append(references_fd)
            info = os.stat(filename, dir_fd=references_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StorageError("unsafe_storage_entry")
            os.unlink(filename, dir_fd=references_fd)
            return True
        finally:
            for opened in reversed(descriptors):
                os.close(opened)
