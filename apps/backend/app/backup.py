"""Explicit encrypted studio backup and isolated restore; never starts/stops services."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

FORMAT = "murrcloud-video-backup-v2"
MIB = 1024**2
GIB = 1024**3


class BackupError(Exception):
    """Safe operator error; no connection URLs, SQL, keys or tool stderr."""


@dataclass(frozen=True)
class BackupLimits:
    max_database_bytes: int = GIB
    max_media_bytes: int = 2 * GIB
    max_archive_bytes: int = 4 * GIB
    max_manifest_bytes: int = 16 * MIB
    max_assets: int = 100_000
    min_free_bytes: int = 15 * GIB
    timeout_seconds: int = 900
    require_separate_filesystem: bool = True

    def __post_init__(self):
        for name in (
            "max_database_bytes",
            "max_media_bytes",
            "max_archive_bytes",
            "max_manifest_bytes",
            "max_assets",
            "timeout_seconds",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise BackupError("invalid_backup_limits")
        if (
            self.min_free_bytes < 0
            or self.max_archive_bytes > 16 * GIB
            or self.max_assets > 100_000
        ):
            raise BackupError("invalid_backup_limits")


def database_url(value: str, *, restoring=False):
    try:
        parsed = make_url(value)
        pattern = (
            r"video_studio_restore_[a-z0-9_]{8,64}"
            if restoring
            else r"video_studio(?:_[a-z0-9_]{1,80})?"
        )
        if (
            parsed.drivername != "postgresql+psycopg"
            or not parsed.host
            or not parsed.username
            or not re.fullmatch(pattern, parsed.database or "")
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or set(parsed.query) - {"sslmode", "sslrootcert"}
        ):
            raise ValueError
        return parsed
    except Exception:
        raise BackupError(
            "unsafe_restore_database" if restoring else "unsafe_backup_database"
        ) from None


def _engine(value):
    database_url(value)
    return create_engine(
        value,
        pool_size=1,
        max_overflow=0,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=600000 -c lock_timeout=10000 -c idle_in_transaction_session_timeout=0",
        },
    )


def _pg_env(url):
    parsed = database_url(url)
    env = {
        "PATH": os.defpath,
        "PGHOST": parsed.host,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": parsed.username,
        "PGDATABASE": parsed.database,
        "PGPASSWORD": parsed.password or "",
        "PGPASSFILE": "/nonexistent",
        "PGCONNECT_TIMEOUT": "5",
        "PGOPTIONS": "-c statement_timeout=600000 -c lock_timeout=10000",
    }
    for key in ("sslmode", "sslrootcert"):
        if key in parsed.query:
            env["PG" + key.upper()] = parsed.query[key]
    return env


def _tool(name):
    found = shutil.which(os.environ.get(name.upper().replace("-", "_"), name))
    if not found:
        raise BackupError("required_backup_tool_missing")
    return found


def _private_dir(value, *, separate=False):
    path = Path(value).absolute()
    try:
        info = path.lstat()
        if (
            path.resolve() != path
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise ValueError
        if separate and info.st_dev == Path("/").stat().st_dev:
            raise BackupError("separate_backup_filesystem_required")
        return path
    except BackupError:
        raise
    except Exception:
        raise BackupError("private_unlinked_directory_required") from None


def _regular(value, maximum):
    path = Path(value).absolute()
    try:
        info = path.lstat()
        if (
            path.resolve() != path
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise ValueError
        return path
    except Exception:
        raise BackupError("unsafe_backup_file") from None


def _reserve(path, minimum, additional=0):
    info = os.statvfs(path)
    if info.f_bavail * info.f_frsize < minimum + additional:
        raise BackupError("backup_capacity_exhausted")


def _digest(path, maximum):
    path = _regular(path, maximum)
    size, digest = 0, hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(MIB):
            size += len(chunk)
            if size > maximum:
                raise BackupError("backup_size_limit")
            digest.update(chunk)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_directory(source, target):
    """Atomically publish new storage without ever replacing an existing name."""
    library = ctypes.CDLL(None, use_errno=True)
    if hasattr(library, "renameat2"):
        function = library.renameat2
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        arguments = (-100, os.fsencode(source), -100, os.fsencode(target), 1)
    elif hasattr(library, "renamex_np"):
        function = library.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(source), os.fsencode(target), 4)
    else:
        raise BackupError("exclusive_directory_publication_unavailable")
    function.restype = ctypes.c_int
    if function(*arguments) != 0:
        raise BackupError("new_restore_storage_publication_failed")
    _fsync_directory(target.parent)


def _copy(source, target, expected, limits):
    source = _regular(source, limits.max_media_bytes)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    size, digest = 0, hashlib.sha256()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with source.open("rb") as incoming, os.fdopen(fd, "wb") as outgoing:
        while chunk := incoming.read(MIB):
            size += len(chunk)
            if size > expected["size_bytes"]:
                raise BackupError("media_changed_during_backup")
            _reserve(target.parent, limits.min_free_bytes, len(chunk))
            outgoing.write(chunk)
            digest.update(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if size != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
        raise BackupError("media_changed_during_backup")


def media_key(key, project_id=None):
    try:
        parts = key.split("/")
        if str(UUID(parts[0])) != parts[0] or (
            project_id is not None and parts[0] != str(project_id)
        ):
            raise ValueError
        if len(parts) == 2:
            extensions = {"mp4"}
        elif len(parts) == 3 and parts[1] in {"references", "productions"}:
            extensions = (
                {"jpg", "png"} if parts[1] == "references" else {"mp4", "mp3", "wav", "srt"}
            )
        else:
            raise ValueError
        stem, extension = parts[-1].rsplit(".", 1)
        if str(UUID(stem)) != stem or extension not in extensions:
            raise ValueError
        return key
    except Exception:
        raise BackupError("unsafe_media_key") from None


@contextmanager
def _quota_lock(root):
    fd = os.open(root / ".quota.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+b") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError("unsafe_backup_lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("studio_storage_busy") from None
        yield


def _kill(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _bounded_command(arguments, maximum, limits):
    return [
        sys.executable,
        str(Path(__file__).with_name("backup_child.py")),
        str(os.getpid()),
        str(maximum),
        str(limits.timeout_seconds),
        *arguments,
    ]


def _capture(arguments, target, env, maximum, limits):
    """Bound stdout, disk reserve and wall time without retaining tool stderr."""
    error, stop = [], threading.Event()
    with target.open("xb") as destination:
        os.chmod(target, 0o600)
        process = subprocess.Popen(
            _bounded_command(arguments, maximum, limits),
            stdin=subprocess.DEVNULL,
            stdout=destination,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

        def watch():
            deadline = time.monotonic() + limits.timeout_seconds
            while not stop.wait(0.1):
                try:
                    if time.monotonic() >= deadline:
                        raise BackupError("backup_tool_timeout")
                    if target.stat().st_size > maximum:
                        raise BackupError("backup_size_limit")
                    _reserve(target.parent, limits.min_free_bytes)
                except Exception as exc:
                    error.append(
                        exc if isinstance(exc, BackupError) else BackupError("backup_io_failed")
                    )
                    _kill(process)
                    return

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        try:
            returncode = process.wait(timeout=limits.timeout_seconds + 5)
        except BaseException:
            _kill(process)
            raise
        finally:
            stop.set()
            thread.join(timeout=5)
        if error:
            raise error[0]
        if returncode:
            if target.stat().st_size >= maximum:
                raise BackupError("backup_size_limit")
            raise BackupError("backup_tool_failed")
        destination.flush()
        os.fsync(destination.fileno())
    if target.stat().st_size > maximum:
        raise BackupError("backup_size_limit")


def _tool_major(tool):
    try:
        result = subprocess.run(
            [_tool(tool), "--version"],
            capture_output=True,
            timeout=5,
            env={"PATH": os.defpath},
            check=True,
        )
        match = re.search(rb"PostgreSQL\) (\d+)\.", result.stdout[:512])
        if not match:
            raise ValueError
        return int(match.group(1))
    except Exception:
        raise BackupError("postgresql_tool_version_unavailable") from None


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BackupError("duplicate_manifest_key")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=unique)
    except BackupError:
        raise
    except Exception:
        raise BackupError("invalid_backup_manifest") from None


def _encrypt(stage, files, output, recipient, limits):
    import tarfile

    fd, partial_name = tempfile.mkstemp(prefix=".studio-encrypted-", dir=output.parent)
    partial = Path(partial_name)
    error, stop = [], threading.Event()
    try:
        with os.fdopen(fd, "wb") as destination:
            process = subprocess.Popen(
                _bounded_command(
                    [_tool("age"), "--encrypt", "--recipient", recipient],
                    limits.max_archive_bytes,
                    limits,
                ),
                stdin=subprocess.PIPE,
                stdout=destination,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={"PATH": os.defpath},
            )

            def watch():
                deadline = time.monotonic() + limits.timeout_seconds
                while not stop.wait(0.1):
                    try:
                        if time.monotonic() >= deadline:
                            raise BackupError("backup_tool_timeout")
                        if partial.stat().st_size > limits.max_archive_bytes:
                            raise BackupError("backup_size_limit")
                        _reserve(output.parent, limits.min_free_bytes)
                    except Exception as exc:
                        error.append(
                            exc if isinstance(exc, BackupError) else BackupError("backup_io_failed")
                        )
                        _kill(process)
                        return

            thread = threading.Thread(target=watch, daemon=True)
            thread.start()
            try:
                with tarfile.open(
                    fileobj=process.stdin, mode="w|", format=tarfile.USTAR_FORMAT
                ) as archive:
                    for name in ["manifest.json", *files]:
                        path = stage / name
                        size = path.stat().st_size
                        info = tarfile.TarInfo(name)
                        info.size, info.mode, info.mtime = size, 0o600, 0
                        with path.open("rb") as source:
                            archive.addfile(info, source)
                process.stdin.close()
                code = process.wait(timeout=limits.timeout_seconds + 5)
            except BaseException:
                _kill(process)
                if error:
                    raise error[0] from None
                raise
            finally:
                stop.set()
                thread.join(timeout=5)
            if error:
                raise error[0]
            if code:
                raise BackupError("backup_encryption_failed")
            destination.flush()
            os.fsync(destination.fileno())
        result = _digest(partial, limits.max_archive_bytes)
        os.link(partial, output, follow_symlinks=False)
        _fsync_directory(output.parent)
        return result
    finally:
        partial.unlink(missing_ok=True)


def create_backup(
    database_url_value, storage_dir, output, recipient, staging_dir, *, quiesced=False, limits=None
):
    limits = limits or BackupLimits()
    if quiesced is not True:
        raise BackupError("explicit_quiescence_required")
    parsed = database_url(database_url_value)
    if not isinstance(recipient, str) or not re.fullmatch(r"age1[0-9a-z]{50,100}", recipient):
        raise BackupError("age_public_recipient_required")
    root = _private_dir(storage_dir)
    staging = _private_dir(staging_dir, separate=limits.require_separate_filesystem)
    output = Path(output).absolute()
    _private_dir(output.parent, separate=limits.require_separate_filesystem)
    if (
        output.exists()
        or output.is_symlink()
        or output.resolve() != output
        or not output.name.endswith(".tar.age")
    ):
        raise BackupError("new_encrypted_output_required")
    if staging.is_relative_to(root) or output.is_relative_to(root):
        raise BackupError("backup_destination_inside_source")
    staged_bytes = limits.max_database_bytes + limits.max_media_bytes
    if staging.stat().st_dev == output.parent.stat().st_dev:
        _reserve(staging, limits.min_free_bytes, staged_bytes + limits.max_archive_bytes)
    else:
        _reserve(staging, limits.min_free_bytes, staged_bytes)
        _reserve(output.parent, limits.min_free_bytes, limits.max_archive_bytes)
    created_at = datetime.now(UTC).isoformat()
    engine = _engine(database_url_value)
    try:
        with (
            _quota_lock(root),
            tempfile.TemporaryDirectory(prefix=".studio-backup-", dir=staging) as directory,
        ):
            stage, assets, files = Path(directory), [], {}
            (stage / "media").mkdir(mode=0o700)
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
                with conn.begin():
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    version = (
                        int(conn.execute(text("SHOW server_version_num")).scalar_one()) // 10000
                    )
                    if _tool_major("pg_dump") != version:
                        raise BackupError("postgresql_tool_major_mismatch")
                    revision = conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    from app.migration_head import SCHEMA_HEAD

                    if revision != SCHEMA_HEAD:
                        raise BackupError("backup_schema_not_current")
                    snapshot = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
                    if not re.fullmatch(r"[A-Fa-f0-9-]+", snapshot):
                        raise BackupError("invalid_database_snapshot")
                    rows = (
                        conn.execute(
                            text(
                                "SELECT id,project_id,kind,object_key,size_bytes,sha256,status,storage_backend FROM media_assets WHERE status IN ('ready','pending') ORDER BY id LIMIT :limit"
                            ),
                            {"limit": limits.max_assets + 1},
                        )
                        .mappings()
                        .all()
                    )
                    if len(rows) > limits.max_assets:
                        raise BackupError("backup_asset_count_limit")
                    total = 0
                    for row in rows:
                        if row["storage_backend"] != "local":
                            raise BackupError("remote_asset_backup_not_supported")
                        key = media_key(row["object_key"], row["project_id"])
                        path = root / key
                        exists = path.exists() or path.is_symlink()
                        if not exists and row["status"] == "ready":
                            raise BackupError("ready_media_missing")
                        if exists and (
                            not row["sha256"] or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                        ):
                            raise BackupError("asset_checksum_missing")
                        item = {
                            "id": str(row["id"]),
                            "project_id": str(row["project_id"]),
                            "object_key": key,
                            "size_bytes": row["size_bytes"],
                            "sha256": row["sha256"],
                            "status": row["status"],
                            "included": exists,
                        }
                        assets.append(item)
                        if exists:
                            total += row["size_bytes"]
                            if total > limits.max_media_bytes:
                                raise BackupError("backup_media_size_limit")
                            _copy(path, stage / "media" / key, item, limits)
                            files["media/" + key] = {
                                "size_bytes": row["size_bytes"],
                                "sha256": row["sha256"],
                            }
                    _capture(
                        [
                            _tool("pg_dump"),
                            "--format=custom",
                            "--compress=0",
                            "--no-owner",
                            "--no-acl",
                            "--no-password",
                            "--lock-wait-timeout=10s",
                            "--snapshot=" + snapshot,
                        ],
                        stage / "database.dump",
                        _pg_env(database_url_value),
                        limits.max_database_bytes,
                        limits,
                    )
            files = {
                "database.dump": _digest(stage / "database.dump", limits.max_database_bytes),
                **files,
            }
            manifest = {
                "format": FORMAT,
                "created_at": created_at,
                "database": parsed.database,
                "postgres_major": version,
                "schema_revision": revision,
                "files": files,
                "assets": assets,
            }
            body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            if len(body) > limits.max_manifest_bytes:
                raise BackupError("backup_manifest_size_limit")
            (stage / "manifest.json").write_bytes(body)
            os.chmod(stage / "manifest.json", 0o600)
            result = _encrypt(stage, files, output, recipient, limits)
            return {
                "archive_sha256": result["sha256"],
                "size_bytes": result["size_bytes"],
                "asset_count": len(assets),
                "created_at": created_at,
            }
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_capture_failed") from None
    finally:
        engine.dispose()


def _manifest(data, limits):
    from app.migration_head import SCHEMA_HEAD

    value = _json(data)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "format",
            "created_at",
            "database",
            "postgres_major",
            "schema_revision",
            "files",
            "assets",
        }
        or value["format"] != FORMAT
        or value["schema_revision"] != SCHEMA_HEAD
        or not isinstance(value["created_at"], str)
        or len(value["created_at"]) > 64
        or type(value["postgres_major"]) is not int
        or value["postgres_major"] < 17
        or not isinstance(value["database"], str)
        or not re.fullmatch(r"video_studio(?:_[a-z0-9_]{1,80})?", value["database"])
        or not isinstance(value["files"], dict)
        or not isinstance(value["assets"], list)
        or len(value["assets"]) > limits.max_assets
    ):
        raise BackupError("invalid_backup_manifest")
    try:
        if datetime.fromisoformat(value["created_at"]).tzinfo is None:
            raise ValueError
    except (ValueError, TypeError):
        raise BackupError("invalid_backup_manifest") from None
    expected, media_total, seen = {"database.dump"}, 0, set()
    for item in value["assets"]:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "project_id",
            "object_key",
            "size_bytes",
            "sha256",
            "status",
            "included",
        }:
            raise BackupError("invalid_backup_manifest")
        try:
            UUID(item["id"])
            UUID(item["project_id"])
            media_key(item["object_key"], item["project_id"])
            if (
                item["id"] in seen
                or type(item["size_bytes"]) is not int
                or item["size_bytes"] < 0
                or type(item["included"]) is not bool
                or (
                    item["sha256"] is not None
                    and (
                        not isinstance(item["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                    )
                )
                or item["status"] not in {"pending", "ready"}
                or (item["status"] == "ready" and not item["included"])
            ):
                raise ValueError
            seen.add(item["id"])
            if item["included"]:
                name = "media/" + item["object_key"]
                if name in expected or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"] or ""):
                    raise ValueError
                expected.add(name)
                media_total += item["size_bytes"]
                if value["files"].get(name) != {
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                }:
                    raise ValueError
        except Exception:
            raise BackupError("invalid_backup_manifest") from None
    if set(value["files"]) != expected or media_total > limits.max_media_bytes:
        raise BackupError("invalid_backup_manifest")
    for name, details in value["files"].items():
        maximum = limits.max_database_bytes if name == "database.dump" else limits.max_media_bytes
        if (
            not isinstance(details, dict)
            or set(details) != {"size_bytes", "sha256"}
            or type(details["size_bytes"]) is not int
            or not 0 <= details["size_bytes"] <= maximum
            or not isinstance(details["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", details["sha256"])
        ):
            raise BackupError("invalid_backup_manifest")
    return value


def _octal(field):
    value = field.rstrip(b"\0 ").lstrip(b" ")
    if not value or any(c not in b"01234567" for c in value):
        raise BackupError("unsafe_backup_archive")
    return int(value, 8)


def _exact(source, size):
    body = source.read(size)
    if len(body) != size:
        raise BackupError("truncated_backup_archive")
    return body


def _extract_bundle(bundle, stage, limits):
    """Strict USTAR parser: rejects extensions before a tar library can allocate them."""
    bundle = _regular(bundle, limits.max_archive_bytes)
    seen, manifest, count = set(), None, 0
    with bundle.open("rb") as source:
        while True:
            header = _exact(source, 512)
            if header == bytes(512):
                if _exact(source, 512) != bytes(512):
                    raise BackupError("unsafe_backup_archive")
                while tail := source.read(MIB):
                    if any(tail):
                        raise BackupError("unsafe_backup_archive")
                break
            count += 1
            if (
                count > limits.max_assets + 2
                or header[156:157] not in {b"0", b"\0"}
                or header[257:265] != b"ustar\x0000"
                or any(header[157:257])
                or any(header[345:500])
            ):
                raise BackupError("unsafe_backup_archive")
            expected_checksum = sum(header[:148]) + 8 * ord(" ") + sum(header[156:])
            if _octal(header[148:156]) != expected_checksum:
                raise BackupError("unsafe_backup_archive")
            try:
                name = header[:100].split(b"\0", 1)[0].decode("ascii")
            except Exception:
                raise BackupError("unsafe_backup_archive") from None
            size = _octal(header[124:136])
            if name in seen:
                raise BackupError("duplicate_backup_member")
            seen.add(name)
            if manifest is None:
                if name != "manifest.json" or size > limits.max_manifest_bytes:
                    raise BackupError("manifest_must_be_first")
                manifest = _manifest(_exact(source, size), limits)
            else:
                if name not in manifest["files"] or size != manifest["files"][name]["size_bytes"]:
                    raise BackupError("unexpected_backup_member")
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                checksum = hashlib.sha256()
                with target.open("xb") as destination:
                    os.chmod(target, 0o600)
                    remaining = size
                    while remaining:
                        chunk = _exact(source, min(MIB, remaining))
                        _reserve(stage, limits.min_free_bytes, len(chunk))
                        destination.write(chunk)
                        checksum.update(chunk)
                        remaining -= len(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                if checksum.hexdigest() != manifest["files"][name]["sha256"]:
                    raise BackupError("backup_checksum_mismatch")
            padding = (-size) % 512
            if padding and any(_exact(source, padding)):
                raise BackupError("unsafe_backup_archive")
    if manifest is None or seen != {"manifest.json", *manifest["files"]}:
        raise BackupError("incomplete_backup_archive")
    return manifest


def _identity(path):
    path = _regular(path, 65536)
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise BackupError("private_age_identity_required")
    try:
        keys = [
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not keys or any(not re.fullmatch(r"AGE-SECRET-KEY-1[0-9A-Z]+", key) for key in keys):
            raise ValueError
    except Exception:
        raise BackupError("private_age_identity_required") from None
    return path


def _empty_database(conn):
    # No CREATE/DROP DATABASE is issued by the restore tool. The operator creates
    # the isolated, empty destination using independent credentials beforehand.
    count = conn.execute(
        text("""SELECT count(*) FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
        AND n.nspname NOT LIKE 'pg_temp_%'""")
    ).scalar_one()
    schemas = conn.execute(
        text(
            "SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname NOT IN ('public','pg_catalog','information_schema') AND nspname NOT LIKE 'pg_%' "
        )
    ).scalar_one()
    routines = conn.execute(
        text(
            "SELECT count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'"
        )
    ).scalar_one()
    types = conn.execute(
        text(
            "SELECT count(*) FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public'"
        )
    ).scalar_one()
    large_objects = conn.execute(
        text("SELECT count(*) FROM pg_catalog.pg_largeobject_metadata")
    ).scalar_one()
    if count or schemas or routines or types or large_objects:
        raise BackupError("restore_database_not_empty")


def _fence_restored_jobs(conn):
    revoked = conn.execute(text("DELETE FROM auth_sessions")).rowcount
    heartbeats = conn.execute(text("DELETE FROM worker_heartbeats")).rowcount
    # Restoring a database must not restore usable provider credentials. The
    # encryption master is held separately; fence even already-empty tombstones.
    provider_keys = conn.execute(
        text("SELECT count(*) FROM provider_secrets WHERE encrypted_value IS NOT NULL")
    ).scalar_one()
    conn.execute(
        text("""UPDATE provider_secrets SET encrypted_value=NULL, revision=revision+1,
        updated_at=CURRENT_TIMESTAMP, updated_by=NULL""")
    )
    generation = conn.execute(
        text("""UPDATE generation_jobs SET status='reconciliation_required',
        last_error_code='restore_review_required',dispatch_state='not_needed',dispatch_error_code='restore_review_required'
        WHERE status NOT IN ('succeeded','failed','cancelled','abandoned')""")
    ).rowcount
    director = conn.execute(
        text("""UPDATE director_jobs SET status='reconciliation_required',
        error_code='restore_review_required',dispatch_state='not_needed',dispatch_error_code='restore_review_required'
        WHERE status IN ('approved','submitting','reconciliation_required')""")
    ).rowcount
    renders = conn.execute(
        text("""UPDATE rendering_jobs SET status='failed',error_code='restore_review_required',
        dispatch_state='not_needed',dispatch_error_code='restore_review_required',lease_token=NULL,
        lease_expires_at=NULL,completed_at=CURRENT_TIMESTAMP WHERE status IN ('pending','admitted','running','paused')""")
    ).rowcount
    conn.execute(
        text(
            "UPDATE projects SET status='failed' WHERE status IN ('planning','awaiting_approval','generating','narrating','rendering')"
        )
    )
    from sqlalchemy import insert

    from app.models import AuditEvent

    conn.execute(
        insert(AuditEvent).values(
            event_type="backup.restored_isolated",
            details={
                "sessions_revoked": revoked,
                "worker_heartbeats_cleared": heartbeats,
                "provider_keys_cleared": provider_keys,
                "generation_fenced": generation,
                "director_fenced": director,
                "renders_failed_for_review": renders,
                "workers_started": False,
            },
        )
    )
    return {
        "sessions_revoked": revoked,
        "worker_heartbeats_cleared": heartbeats,
        "provider_keys_cleared": provider_keys,
        "generation_fenced": generation,
        "director_fenced": director,
        "renders_failed_for_review": renders,
    }


def restore_backup(
    database_url_value,
    archive,
    expected_sha256,
    identity,
    storage_target,
    staging_dir,
    *,
    limits=None,
):
    limits = limits or BackupLimits()
    database_url(database_url_value, restoring=True)
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BackupError("trusted_archive_hash_required")
    archive = _regular(archive, limits.max_archive_bytes)
    if _digest(archive, limits.max_archive_bytes)["sha256"] != expected_sha256:
        raise BackupError("encrypted_archive_hash_mismatch")
    identity = _identity(identity)
    staging = _private_dir(staging_dir, separate=limits.require_separate_filesystem)
    target = Path(storage_target).absolute()
    parent = _private_dir(target.parent, separate=limits.require_separate_filesystem)
    if staging.stat().st_dev != parent.stat().st_dev:
        raise BackupError("restore_storage_and_staging_must_share_filesystem")
    if target.exists() or target.is_symlink() or target.resolve() != target:
        raise BackupError("new_restore_storage_required")
    if archive.is_relative_to(target) or staging.is_relative_to(target):
        raise BackupError("unsafe_restore_storage")
    _reserve(
        staging,
        limits.min_free_bytes,
        limits.max_archive_bytes + limits.max_database_bytes + limits.max_media_bytes,
    )
    engine = _engine(database_url_value)
    try:
        with engine.connect() as conn:
            _empty_database(conn)
            target_major = int(conn.execute(text("SHOW server_version_num")).scalar_one()) // 10000
        with tempfile.TemporaryDirectory(prefix=".studio-restore-", dir=staging) as directory:
            stage = Path(directory)
            bundle = stage / "bundle.tar"
            _capture(
                [_tool("age"), "--decrypt", "--identity", str(identity), str(archive)],
                bundle,
                {"PATH": os.defpath},
                limits.max_archive_bytes,
                limits,
            )
            extracted = stage / "validated"
            extracted.mkdir(mode=0o700)
            (extracted / "media").mkdir(mode=0o700)
            manifest = _extract_bundle(bundle, extracted, limits)
            if (
                target_major != manifest["postgres_major"]
                or _tool_major("pg_restore") != target_major
            ):
                raise BackupError("postgresql_tool_major_mismatch")
            # Recheck just before restore. This target must remain exclusively
            # isolated and disconnected from application writers throughout.
            with engine.connect() as conn:
                _empty_database(conn)
            marker = stage / "restore.result"
            _capture(
                [
                    _tool("pg_restore"),
                    "--dbname=" + database_url(database_url_value).database,
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-acl",
                    "--no-password",
                    str(extracted / "database.dump"),
                ],
                marker,
                _pg_env(database_url_value),
                MIB,
                limits,
            )
            with engine.begin() as conn:
                from app.migration_head import SCHEMA_HEAD

                if (
                    conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    != SCHEMA_HEAD
                ):
                    raise BackupError("restored_schema_mismatch")
                rows = (
                    conn.execute(
                        text(
                            "SELECT id,project_id,object_key,size_bytes,sha256,status FROM media_assets WHERE status IN ('ready','pending') ORDER BY id"
                        )
                    )
                    .mappings()
                    .all()
                )
                facts = [
                    {
                        "id": str(r["id"]),
                        "project_id": str(r["project_id"]),
                        "object_key": r["object_key"],
                        "size_bytes": r["size_bytes"],
                        "sha256": r["sha256"],
                        "status": r["status"],
                    }
                    for r in rows
                ]
                expected = (
                    [{k: a[k] for k in facts[0]} for a in manifest["assets"]] if facts else []
                )
                if facts != expected or len(rows) != len(manifest["assets"]):
                    raise BackupError("restored_media_manifest_mismatch")
                result = _fence_restored_jobs(conn)
            media = extracted / "media"
            media.mkdir(mode=0o700, exist_ok=True)
            # Only the fresh filesystem destination is published. A database
            # import failure never overwrites existing storage or live objects.
            _publish_directory(media, target)
            for item in manifest["assets"]:
                if item["included"] and _digest(
                    target / item["object_key"], limits.max_media_bytes
                ) != {"size_bytes": item["size_bytes"], "sha256": item["sha256"]}:
                    raise BackupError("restored_media_checksum_mismatch")
            return {
                **result,
                "asset_count": len(manifest["assets"]),
                "archive_sha256": expected_sha256,
                "workers_started": False,
                "schema_revision": manifest["schema_revision"],
            }
    except BackupError:
        raise
    except Exception:
        raise BackupError("isolated_restore_failed") from None
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--database-url-env", default="VIDEO_DATABASE_URL")
    create.add_argument("--storage-dir", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--recipient", required=True)
    create.add_argument("--staging-dir", required=True)
    create.add_argument("--quiesced", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("--database-url-env", default="VIDEO_RESTORE_DATABASE_URL")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--expected-sha256", required=True)
    restore.add_argument("--identity", required=True)
    restore.add_argument("--storage-target", required=True)
    restore.add_argument("--staging-dir", required=True)
    args = parser.parse_args(argv)
    try:
        value = os.environ.get(args.database_url_env, "")
        if args.command == "create":
            report = create_backup(
                value,
                args.storage_dir,
                args.output,
                args.recipient,
                args.staging_dir,
                quiesced=args.quiesced,
            )
        else:
            report = restore_backup(
                value,
                args.archive,
                args.expected_sha256,
                args.identity,
                args.storage_target,
                args.staging_dir,
            )
        print(json.dumps(report, sort_keys=True))
    except BackupError as exc:
        parser.exit(1, "Studio backup operation refused: " + str(exc) + "\n")
    except Exception:
        parser.exit(1, "Studio backup operation refused: backup_operation_failed\n")


if __name__ == "__main__":
    main()
