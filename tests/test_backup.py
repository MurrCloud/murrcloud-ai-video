"""Backup parser, destination and process guards; no provider or database network."""

import fcntl
import hashlib
import io
import json
import logging
import os
import sys
import tarfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from app import backup
from app.migration_head import SCHEMA_HEAD


@pytest.fixture
def limits():
    return backup.BackupLimits(
        max_database_bytes=backup.MIB,
        max_media_bytes=backup.MIB,
        max_archive_bytes=4 * backup.MIB,
        max_manifest_bytes=65536,
        min_free_bytes=0,
        timeout_seconds=2,
        require_separate_filesystem=False,
    )


def private(path):
    path.mkdir(mode=0o700)
    return path.resolve()


def bundle_fixture():
    project, asset = uuid4(), uuid4()
    key = f"{project}/productions/{asset}.srt"
    files = {
        "database.dump": b"fixture-database-not-executable",
        "media/" + key: b"private subtitles",
    }
    manifest = {
        "format": backup.FORMAT,
        "created_at": "2026-11-07T00:00:00+00:00",
        "database": "video_studio_backup_fixture",
        "postgres_major": 17,
        "schema_revision": SCHEMA_HEAD,
        "files": {
            name: {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in files.items()
        },
        "assets": [
            {
                "id": str(asset),
                "project_id": str(project),
                "object_key": key,
                "size_bytes": len(files["media/" + key]),
                "sha256": hashlib.sha256(files["media/" + key]).hexdigest(),
                "status": "ready",
                "included": True,
            }
        ],
    }
    return manifest, files


def write_bundle(path, manifest, files, extras=()):
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in [("manifest.json", json.dumps(manifest).encode()), *files.items()]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info, data in extras:
            archive.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize(
    "value",
    [
        "postgresql+psycopg://user:password@localhost/unrelated_app",
        "postgresql+psycopg://user:password@localhost/postgres",
        "postgresql+psycopg://user:password@localhost/video_studio?options=-csearch_path=erp",
        "sqlite:///video_studio",
        "postgresql+psycopg:///video_studio",
    ],
)
def test_database_allowlist_does_not_reveal_credentials(value):
    with pytest.raises(backup.BackupError, match="^unsafe_backup_database$"):
        backup.database_url(value)


@pytest.mark.parametrize(
    "database", ["video_studio", "video_studio_test", "video_studio_restore_a", "unrelated_app"]
)
def test_restore_database_must_have_isolated_name(database):
    with pytest.raises(backup.BackupError, match="unsafe_restore_database"):
        backup.database_url(f"postgresql+psycopg://u:p@postgres/{database}", restoring=True)


def test_database_credentials_only_passed_in_minimal_child_environment(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "never-pass-provider-key")
    monkeypatch.setenv("PGSERVICE", "never-use-ambient-service")
    env = backup._pg_env(
        "postgresql+psycopg://studio:db-secret@postgres:5433/video_studio?sslmode=require"
    )
    assert env["PGPASSWORD"] == "db-secret" and env["PGDATABASE"] == "video_studio"
    assert env["PGHOST"] == "postgres" and env["PGPORT"] == "5433"
    assert env["PGSSLMODE"] == "require" and env["PGPASSFILE"] == "/nonexistent"
    assert "FAL_KEY" not in env and "PGSERVICE" not in env


def test_backup_requires_explicit_quiescence_before_any_io(limits):
    with pytest.raises(backup.BackupError, match="explicit_quiescence_required"):
        backup.create_backup(
            "invalid", "/missing", "/missing.tar.age", "invalid", "/missing", limits=limits
        )


def test_private_paths_and_unlinked_files_only(tmp_path):
    root = private(tmp_path / "private")
    assert backup._private_dir(root) == root
    root.chmod(0o755)
    with pytest.raises(backup.BackupError, match="private_unlinked_directory_required"):
        backup._private_dir(root)
    root.chmod(0o700)
    source = root / "source"
    source.write_bytes(b"private")
    linked = root / "linked"
    os.link(source, linked)
    with pytest.raises(backup.BackupError, match="unsafe_backup_file"):
        backup._regular(source, 100)
    linked.unlink()
    linked.symlink_to(source)
    with pytest.raises(backup.BackupError, match="unsafe_backup_file"):
        backup._regular(linked, 100)


@pytest.mark.parametrize(
    "key",
    ["../../etc/passwd", "/etc/passwd", f"{uuid4()}/../secret.mp4", f"{uuid4()}/{uuid4()}.env"],
)
def test_media_keys_reject_uncontrolled_paths(key):
    with pytest.raises(backup.BackupError, match="unsafe_media_key"):
        backup.media_key(key)


def test_backup_lock_refuses_active_publisher(tmp_path):
    root = private(tmp_path / "storage")
    with (root / ".quota.lock").open("wb") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(backup.BackupError, match="studio_storage_busy"),
            backup._quota_lock(root),
        ):
            pytest.fail("backup must not overlap publication")


def test_manifest_and_full_bundle_roundtrip(tmp_path, limits):
    manifest, files = bundle_fixture()
    stage = private(tmp_path / "stage")
    archive = write_bundle(tmp_path / "bundle.tar", manifest, files)
    assert backup._extract_bundle(archive, stage, limits) == manifest
    for name, data in files.items():
        assert (stage / name).read_bytes() == data
        assert (stage / name).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "kind",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.DIRTYPE,
        tarfile.CHRTYPE,
        tarfile.XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
    ],
)
def test_archive_extensions_links_and_devices_refused_before_extraction(tmp_path, limits, kind):
    manifest, files = bundle_fixture()
    evil = tarfile.TarInfo("untrusted")
    evil.type = kind
    if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
        evil.linkname = "/etc/passwd"
    archive = write_bundle(tmp_path / "bundle.tar", manifest, files, [(evil, b"")])
    with pytest.raises(backup.BackupError, match="unsafe_backup_archive"):
        backup._extract_bundle(archive, private(tmp_path / "stage"), limits)
    assert not (tmp_path / "stage" / "untrusted").exists()


@pytest.mark.parametrize("name", ["database.dump", "../../escape", "/absolute", ".env"])
def test_archive_duplicate_and_unexpected_members_refused(tmp_path, limits, name):
    manifest, files = bundle_fixture()
    evil = tarfile.TarInfo(name)
    archive = write_bundle(tmp_path / "bundle.tar", manifest, files, [(evil, b"")])
    with pytest.raises(
        backup.BackupError, match="duplicate_backup_member|unexpected_backup_member"
    ):
        backup._extract_bundle(archive, private(tmp_path / "stage"), limits)
    assert not (tmp_path / "escape").exists()


def test_archive_checksum_and_truncation_refused(tmp_path, limits):
    manifest, files = bundle_fixture()
    bad = dict(files)
    bad["database.dump"] = b"x" * len(bad["database.dump"])
    archive = write_bundle(tmp_path / "checksum.tar", manifest, bad)
    with pytest.raises(backup.BackupError, match="backup_checksum_mismatch"):
        backup._extract_bundle(archive, private(tmp_path / "stage1"), limits)
    archive = write_bundle(tmp_path / "truncated.tar", manifest, files)
    archive.write_bytes(archive.read_bytes()[:800])
    with pytest.raises(backup.BackupError, match="truncated_backup_archive"):
        backup._extract_bundle(archive, private(tmp_path / "stage2"), limits)


def test_manifest_duplicates_limits_unknown_files_and_paths_refused(limits):
    manifest, files = bundle_fixture()
    body = json.dumps(manifest).encode()
    with pytest.raises(backup.BackupError, match="duplicate_manifest_key"):
        backup._manifest(body[:-1] + b',"format":"duplicate"}', limits)
    with pytest.raises(backup.BackupError, match="invalid_backup_manifest"):
        backup._manifest(body, replace(limits, max_media_bytes=1))
    manifest["assets"][0]["object_key"] = "../../.env"
    with pytest.raises(backup.BackupError, match="invalid_backup_manifest"):
        backup._manifest(json.dumps(manifest).encode(), limits)
    manifest, _ = bundle_fixture()
    manifest["files"][".env"] = manifest["files"]["database.dump"]
    with pytest.raises(backup.BackupError, match="invalid_backup_manifest"):
        backup._manifest(json.dumps(manifest).encode(), limits)


def test_restore_rejects_wrong_hash_and_existing_storage_before_database(
    tmp_path, limits, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("destination checks must precede database connection")

    monkeypatch.setattr(backup, "_engine", forbidden)
    archive = tmp_path / "backup.tar.age"
    archive.write_bytes(b"not trusted")
    identity = tmp_path / "identity"
    identity.write_text("AGE-SECRET-KEY-1OFFLINEFIXTURE\n")
    identity.chmod(0o600)
    stage = private(tmp_path / "stage")
    existing = private(tmp_path / "existing")
    url = "postgresql+psycopg://studio:secret@postgres/video_studio_restore_testfixture"
    with pytest.raises(backup.BackupError, match="encrypted_archive_hash_mismatch"):
        backup.restore_backup(url, archive, "0" * 64, identity, existing, stage, limits=limits)
    tmp_path.chmod(0o700)
    with pytest.raises(backup.BackupError, match="new_restore_storage_required"):
        backup.restore_backup(
            url,
            archive,
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            identity,
            existing,
            stage,
            limits=limits,
        )


def test_atomic_media_publication_preserves_existing_empty_directory(tmp_path):
    source, target = private(tmp_path / "source"), private(tmp_path / "target")
    (source / "private").write_text("fixture")
    with pytest.raises(backup.BackupError, match="new_restore_storage_publication_failed"):
        backup._publish_directory(source, target)
    assert source.exists() and list(target.iterdir()) == []
    target.rmdir()
    backup._publish_directory(source, target)
    assert (target / "private").read_text() == "fixture" and not source.exists()


def test_tool_output_and_deadline_limits(tmp_path, limits):
    with pytest.raises(backup.BackupError, match="backup_size_limit"):
        backup._capture(
            [sys.executable, "-c", "print('x'*10000)"], tmp_path / "large", {}, 10, limits
        )
    with pytest.raises(backup.BackupError, match="backup_tool_timeout"):
        backup._capture(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path / "slow",
            {},
            100,
            replace(limits, timeout_seconds=1),
        )


def test_cli_never_echoes_database_exception_or_credentials(monkeypatch, capsys):
    monkeypatch.setenv("VIDEO_DATABASE_URL", "connection-secret-never-print")

    def failed(*args, **kwargs):
        raise RuntimeError("connection-secret-never-print")

    monkeypatch.setattr(backup, "create_backup", failed)
    with pytest.raises(SystemExit):
        backup.main(
            [
                "create",
                "--storage-dir",
                "/private",
                "--output",
                "/private/a.tar.age",
                "--recipient",
                "public",
                "--staging-dir",
                "/private",
                "--quiesced",
            ]
        )
    assert "connection-secret" not in capsys.readouterr().err


def test_embedded_backup_migration_preserves_application_security_notices(caplog):
    """Execute the actual env.py without changing the host's logging graph."""
    from app.ingestion_api import logger

    root = Path(__file__).resolve().parents[1]
    output = io.StringIO()
    configuration = Config(str(root / "apps/backend/alembic.ini"), output_buffer=output)
    configuration.set_main_option("script_location", str(root / "apps/backend/alembic"))
    # Offline compilation executes the migration environment and all revisions
    # without opening this deliberately unused connection.
    configuration.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://unused:unused@localhost/unused"
    )
    configuration.attributes["configure_logger"] = False
    with caplog.at_level(logging.WARNING, logger=logger.name):
        before = (logger.disabled, logger.level, logger.propagate, tuple(logger.handlers))
        command.upgrade(configuration, "head", sql=True)
        assert (logger.disabled, logger.level, logger.propagate, tuple(logger.handlers)) == before
        logger.warning("Fixture security notice: operator reconciliation required")
    assert "CREATE TABLE projects" in output.getvalue()
    assert "Fixture security notice: operator reconciliation required" in caplog.text


def test_restore_clears_saved_provider_keys_and_advances_tombstones(api):
    from app.backup import _fence_restored_jobs
    from app.models import AuditEvent, ProviderSecret
    from sqlalchemy import select

    with api.sessions() as db:
        db.add(
            ProviderSecret(
                provider="openai",
                encrypted_value="opaque-encrypted-test-fixture",
                revision=3,
                updated_by=api.user_ids["admin"],
            )
        )
        db.add(ProviderSecret(provider="fal", revision=7))
        db.commit()
    with api.app.state.engine.begin() as connection:
        result = _fence_restored_jobs(connection)
    assert result["provider_keys_cleared"] == 1
    with api.sessions() as db:
        saved = db.get(ProviderSecret, "openai")
        assert saved.encrypted_value is None and saved.revision == 4 and saved.updated_by is None
        assert db.get(ProviderSecret, "fal").revision == 8
        event = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "backup.restored_isolated")
        )
        assert event.details["provider_keys_cleared"] == 1
        assert "opaque-encrypted-test-fixture" not in str(event.details)
