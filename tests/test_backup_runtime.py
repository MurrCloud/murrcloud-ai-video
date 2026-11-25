"""Real encrypted PostgreSQL/media rehearsal in disposable Compose only.

Creates and removes only randomly named databases owned by this test. It never
uses or replaces the service's main database, ERP, live providers or real media.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from app import backup
from app.catalog import seed_catalog
from app.db import make_session_factory
from app.models import (
    ActualUsageCost,
    AuthSession,
    GenerationAttempt,
    GenerationJob,
    MediaAsset,
    Project,
    ProjectSettings,
    ProviderSecret,
    RenderingJob,
    Scene,
    SceneVersion,
    User,
    WorkerHeartbeat,
)
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.mark.integration
def test_encrypted_database_and_private_media_restore_with_paid_job_fencing(tmp_path, monkeypatch):
    if os.environ.get("VIDEO_TEST_BACKUP_ENABLED") != "true":
        pytest.skip("Set VIDEO_TEST_BACKUP_ENABLED=true in disposable Compose")
    value = os.environ.get("VIDEO_LIVE_DATABASE_URL", "")
    parsed = make_url(value)
    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.host == "postgres" and parsed.database == "video_studio", "Compose only"
    for tool in ("age", "age-keygen", "pg_dump", "pg_restore"):
        assert shutil.which(tool), f"Test image must include {tool}"
    assert backup._tool_major("pg_dump") == backup._tool_major("pg_restore") == 17

    tmp_path.chmod(0o700)
    root = tmp_path.resolve()
    storage, staging, recovered_parent = (root / name for name in ("source", "staging", "recovery"))
    for path in (storage, staging, recovered_parent):
        path.mkdir(mode=0o700)
    identity = root / "identity"
    subprocess.run(
        ["age-keygen", "-o", str(identity)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    identity.chmod(0o600)
    recipient = (
        subprocess.run(
            ["age-keygen", "-y", str(identity)], check=True, capture_output=True, timeout=10
        )
        .stdout.decode()
        .strip()
    )
    limits = backup.BackupLimits(
        max_database_bytes=16 * backup.MIB,
        max_media_bytes=4 * backup.MIB,
        max_archive_bytes=32 * backup.MIB,
        max_manifest_bytes=backup.MIB,
        min_free_bytes=backup.MIB,
        timeout_seconds=90,
        require_separate_filesystem=False,
    )
    identifier = uuid4().hex
    source_name, restore_name = (
        "video_studio_backup_" + identifier,
        "video_studio_restore_" + identifier,
    )
    admin = create_engine(
        parsed.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
    created, source, restored = [], None, None
    try:
        with admin.connect() as conn:
            for name in (source_name, restore_name):
                assert re.fullmatch(r"video_studio_(backup|restore)_[a-f0-9]{32}", name)
                conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
                created.append(name)
        source_url = parsed.set(database=source_name).render_as_string(hide_password=False)
        restore_url = parsed.set(database=restore_name).render_as_string(hide_password=False)
        source, restored = backup._engine(source_url), backup._engine(restore_url)
        configuration = Config(
            str(Path(__file__).resolve().parents[1] / "apps/backend/alembic.ini")
        )
        configuration.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "apps/backend/alembic")
        )
        configuration.attributes["configure_logger"] = False
        with source.begin() as conn:
            configuration.attributes["connection"] = conn
            command.upgrade(configuration, "head")

        sessions = make_session_factory(source)
        with sessions() as db:
            seed_catalog(db)
            user = User(
                email="backup-fixture@example.com", password_hash="offline-fixture", role="editor"
            )
            db.add(user)
            db.flush()
            staged = db.get(ProviderSecret, "openai")
            staged.encrypted_value = (
                Fernet(Fernet.generate_key())
                .encrypt(
                    json.dumps(
                        {
                            "format": "studio-provider-key-v1",
                            "provider": "openai",
                            "key": "offline-backup-key-123456789",
                        }
                    ).encode()
                )
                .decode()
            )
            staged.revision = 3
            staged.updated_by = user.id
            project = Project(
                owner_id=user.id,
                name="Original private project",
                status="generating",
                budget_usd=Decimal("10"),
                approved_cap_usd=Decimal("2"),
                committed_cost_usd=Decimal("2"),
            )
            db.add(project)
            db.flush()
            db.add(ProjectSettings(project_id=project.id))
            model_id = "fal-ai/wan/v2.7/text-to-video"
            scene = Scene(
                project_id=project.id,
                scene_key="scene_001",
                order_index=1,
                duration_seconds=5,
                video_model_id=model_id,
                visual_prompt="Offline restoration fixture",
            )
            db.add(scene)
            db.flush()
            version = SceneVersion(
                scene_id=scene.id,
                version_number=1,
                video_model_id=model_id,
                generation_mode="text_to_video",
                duration_seconds=5,
                visual_prompt=scene.visual_prompt,
            )
            db.add(version)
            db.flush()
            job = GenerationJob(
                project_id=project.id,
                scene_version_id=version.id,
                model_id=model_id,
                provider="fal",
                endpoint_id=model_id,
                provider_job_id="offline-existing-request",
                idempotency_key="backup-intent-fixture",
                status="submitted",
                reserved_cost_usd=Decimal("2"),
            )
            db.add(job)
            db.flush()
            db.add(
                GenerationAttempt(
                    job_id=job.id,
                    attempt_number=1,
                    operation="submit",
                    status="ambiguous",
                    provider_request_id="offline-existing-request",
                )
            )
            db.add(
                ActualUsageCost(
                    project_id=project.id,
                    generation_job_id=job.id,
                    provider="fal",
                    amount_usd=None,
                    status="unavailable",
                    usage_details={"paid_intent": True},
                )
            )
            db.add(
                AuthSession(
                    user_id=user.id,
                    token_hash="1" * 64,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            db.add(
                WorkerHeartbeat(
                    worker_id="offline-source-worker-fixture",
                    queue="rendering",
                    last_seen_at=datetime.now(UTC),
                )
            )
            db.add(
                RenderingJob(
                    project_id=project.id, idempotency_key="backup-render-fixture", status="running"
                )
            )
            asset_id, pending_id = uuid4(), uuid4()
            media = b"private original scene fixture, never real customer media"
            key = f"{project.id}/{asset_id}.mp4"
            (storage / str(project.id)).mkdir(mode=0o700)
            (storage / key).write_bytes(media)
            (storage / ".env").write_text("offline-secret-fixture-excluded-from-bundle")
            db.add(
                MediaAsset(
                    id=asset_id,
                    project_id=project.id,
                    kind="clip",
                    object_key=key,
                    mime_type="video/mp4",
                    size_bytes=len(media),
                    sha256=hashlib.sha256(media).hexdigest(),
                    status="ready",
                )
            )
            db.add(
                MediaAsset(
                    id=pending_id,
                    project_id=project.id,
                    kind="clip",
                    object_key=f"{project.id}/{pending_id}.mp4",
                    mime_type="video/mp4",
                    size_bytes=0,
                    status="pending",
                )
            )
            db.commit()
            project_id = project.id

        # Change the source while pg_dump runs: the exported read-only snapshot
        # must preserve the same earlier DB view used to select copied media.
        original_capture = backup._capture
        changed = False

        def capture(arguments, *args, **kwargs):
            nonlocal changed
            if Path(arguments[0]).name == "pg_dump" and not changed:
                with source.begin() as conn:
                    conn.execute(
                        text("UPDATE projects SET name='Changed after snapshot' WHERE id=:id"),
                        {"id": project_id},
                    )
                changed = True
            return original_capture(arguments, *args, **kwargs)

        monkeypatch.setattr(backup, "_capture", capture)
        archive = root / "studio.tar.age"
        report = backup.create_backup(
            source_url, storage, archive, recipient, staging, quiesced=True, limits=limits
        )
        assert changed and report["asset_count"] == 2
        ciphertext = archive.read_bytes()
        assert ciphertext.startswith(b"age-encryption.org/v1") and media not in ciphertext
        assert report["archive_sha256"] == hashlib.sha256(ciphertext).hexdigest()
        assert archive.stat().st_mode & 0o077 == 0
        destination = recovered_parent / "media"
        result = backup.restore_backup(
            restore_url,
            archive,
            report["archive_sha256"],
            identity,
            destination,
            staging,
            limits=limits,
        )
        assert result["workers_started"] is False
        assert result["worker_heartbeats_cleared"] == 1
        assert result["provider_keys_cleared"] == 1
        assert (
            result["sessions_revoked"]
            == result["generation_fenced"]
            == result["renders_failed_for_review"]
            == 1
        )
        assert (destination / key).read_bytes() == media
        assert not (destination / ".env").exists()
        assert destination.stat().st_mode & 0o077 == 0
        with restored.connect() as conn:
            assert (
                conn.execute(text("SELECT name FROM projects")).scalar_one()
                == "Original private project"
            )
            assert conn.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM worker_heartbeats")).scalar_one() == 0
            assert (
                conn.execute(
                    text("SELECT count(*) FROM provider_secrets WHERE encrypted_value IS NOT NULL")
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT revision FROM provider_secrets WHERE provider='openai'")
                ).scalar_one()
                == 4
            )
            assert (
                conn.execute(
                    text("SELECT revision FROM provider_secrets WHERE provider='fal'")
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM provider_secrets WHERE updated_by IS NOT NULL")
                ).scalar_one()
                == 0
            )
            row = conn.execute(
                text(
                    "SELECT status,dispatch_state,provider_job_id,reserved_cost_usd FROM generation_jobs"
                )
            ).one()
            assert tuple(row) == (
                "reconciliation_required",
                "not_needed",
                "offline-existing-request",
                Decimal("2"),
            )
            assert (
                conn.execute(text("SELECT status FROM generation_attempts")).scalar_one()
                == "ambiguous"
            )
            assert (
                conn.execute(text("SELECT amount_usd FROM actual_usage_costs")).scalar_one() is None
            )
            assert conn.execute(
                text("SELECT committed_cost_usd FROM projects")
            ).scalar_one() == Decimal("2")
            assert conn.execute(
                text("SELECT approved_cap_usd FROM projects")
            ).scalar_one() == Decimal("2")
            assert (
                conn.execute(text("SELECT error_code FROM rendering_jobs")).scalar_one()
                == "restore_review_required"
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM media_assets WHERE status='pending'")
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM audit_events WHERE event_type='backup.restored_isolated'"
                    )
                ).scalar_one()
                == 1
            )
        with source.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one() == 1
            assert (
                conn.execute(
                    text("SELECT count(*) FROM provider_secrets WHERE encrypted_value IS NOT NULL")
                ).scalar_one()
                == 1
            )
            assert conn.execute(text("SELECT count(*) FROM worker_heartbeats")).scalar_one() == 1
            assert (
                conn.execute(text("SELECT status FROM generation_jobs")).scalar_one() == "submitted"
            )
        # Existing nonempty DB is protected even if a fresh media name is used.
        with pytest.raises(backup.BackupError, match="restore_database_not_empty"):
            backup.restore_backup(
                restore_url,
                archive,
                report["archive_sha256"],
                identity,
                recovered_parent / "must-not-exist",
                staging,
                limits=limits,
            )
        assert not (recovered_parent / "must-not-exist").exists()
        assert list(staging.iterdir()) == []
    finally:
        for engine in (source, restored):
            if engine is not None:
                engine.dispose()
        with admin.connect() as conn:
            for name in reversed(created):
                assert name in (source_name, restore_name)
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name AND pid<>pg_backend_pid()"
                    ),
                    {"name": name},
                )
                conn.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()
