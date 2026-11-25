"""Actual disposable render queue with synthetic clips and synthetic admission data.

This is not a provider generation, speech-service, VPS or ERP workload test.
"""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.host_guard import require_admission
from app.media import probe_media, validate_output
from app.models import (
    ActualUsageCost,
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    MediaAsset,
    Project,
    ProjectSettings,
    RenderingJob,
    Scene,
    SceneVersion,
    User,
    VideoModel,
)
from app.production import create_render
from app.production_schemas import ProductionSettings, RenderIn
from app.production_storage import artifact_path
from app.production_tasks import prepare_render
from app.security import hash_password
from celery import Celery
from sqlalchemy import delete, func, select

SHARED = Path("/var/lib/video")
SIGNAL = SHARED / "ci-host-signal.json"
WAN = "fal-ai/wan/v2.7/text-to-video"


def synthetic_signal(*, healthy):
    """Only a protocol fixture: these values are not observed host/ERP metrics."""
    return {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "memory_available_bytes": 4 * 1024**3 if healthy else 1,
        "root_free_bytes": 30 * 1024**3,
        "cpu_count": 2,
        "load1": 0.1,
        "services": {"erp": True, "postgresql": True, "nginx": True},
        "erp_http_status": 200,
        "erp_response_ms": 50,
    }


def write_signal(healthy):
    temporary = SIGNAL.with_suffix(".tmp")
    temporary.write_text(json.dumps(synthetic_signal(healthy=healthy)), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, SIGNAL)


@pytest.mark.integration
def test_real_render_worker_admission_queue_and_valid_mp4():
    if os.environ.get("VIDEO_TEST_RENDER_WORKER_ENABLED") != "true":
        pytest.skip("Disposable shared-storage rendering worker is not enabled")
    url, broker = os.environ["VIDEO_LIVE_DATABASE_URL"], os.environ["VIDEO_LIVE_REDIS_URL"]
    assert urlsplit(url).hostname == "postgres" and urlsplit(url).path == "/video_studio"
    assert urlsplit(broker).hostname == "redis" and urlsplit(broker).path == "/0"
    assert SHARED.resolve() == SHARED and SHARED.is_dir()
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=url,
        redis_url=broker,
        session_secret=secrets.token_hex(32),
        allowed_origins=["http://localhost:8100"],
        cookie_secure=False,
        render_enabled=True,
        narration_enabled=False,
        paid_generation_enabled=False,
        director_enabled=False,
        fal_key="",
        openai_key="",
        storage_dir=SHARED,
        temp_dir=Path("/tmp/ci-render-prepare"),
        host_signal_path=SIGNAL,
    )
    engine = make_engine(url)
    sessions = make_session_factory(engine)
    client = Celery("studio-real-render-ci", broker=broker)
    client.conf.update(
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        broker_connection_timeout=3,
        broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    )
    stop, heartbeat = threading.Event(), None
    project_id, user_id, render_id = uuid4(), uuid4(), None
    folder = SHARED / str(project_id)
    folder.mkdir(mode=0o700)
    source_id = uuid4()
    clip = folder / f"{source_id}.mp4"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-f",
                "lavfi",
                "-i",
                "color=c=0xFF7D00:s=640x360:r=30:d=2",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t",
                "2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                "-c:a",
                "aac",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(clip),
            ],
            check=True,
            timeout=15,
            capture_output=True,
        )
        os.chmod(clip, 0o600)
        clip_digest = hashlib.sha256(clip.read_bytes()).hexdigest()
        options = ProductionSettings(
            narration_enabled=False, burn_subtitles=False, allow_upscale=True
        )
        with sessions() as db:
            assert db.scalar(select(VideoModel).where(VideoModel.model_id == WAN)) is not None
            user = User(
                id=user_id,
                email=f"render-ci-{uuid4().hex}@example.com",
                role="editor",
                password_hash=hash_password(secrets.token_urlsafe(24)),
            )
            project = Project(
                id=project_id,
                owner_id=user_id,
                name="Synthetic render queue fixture",
                target_duration_seconds=2,
                budget_usd=0,
            )
            db.add(user)
            db.flush()
            db.add(project)
            db.flush()
            db.add(
                ProjectSettings(
                    project_id=project_id,
                    narration_enabled=False,
                    composition={"production": options.model_dump(mode="json")},
                )
            )
            scene = Scene(
                project_id=project_id,
                scene_key="scene_synthetic",
                order_index=1,
                duration_seconds=2,
                video_model_id=WAN,
                visual_prompt="Synthetic orange fixture",
                transition="cut",
                status="completed",
                generation_settings={
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                    "audio_enabled": True,
                },
            )
            db.add(scene)
            db.flush()
            version = SceneVersion(
                scene_id=scene.id,
                version_number=1,
                video_model_id=WAN,
                generation_mode="text_to_video",
                duration_seconds=2,
                visual_prompt=scene.visual_prompt,
                status="completed",
            )
            db.add(version)
            db.flush()
            asset = MediaAsset(
                id=source_id,
                project_id=project_id,
                scene_version_id=version.id,
                kind="clip",
                object_key=f"{project_id}/{source_id}.mp4",
                mime_type="video/mp4",
                size_bytes=clip.stat().st_size,
                sha256=clip_digest,
                status="ready",
                metadata_json={"width": 640, "height": 360, "fixture": "synthetic"},
            )
            db.add(asset)
            db.flush()
            db.add(
                GenerationJob(
                    project_id=project_id,
                    scene_version_id=version.id,
                    model_id=WAN,
                    provider="fal",
                    endpoint_id=WAN,
                    idempotency_key=uuid4().hex,
                    status="succeeded",
                    output_asset_id=source_id,
                    request_snapshot={
                        **scene.generation_settings,
                        "model_id": WAN,
                        "prompt": scene.visual_prompt,
                        "duration_seconds": 2,
                        "mode": "text_to_video",
                        "negative_prompt": "",
                    },
                )
            )
            db.commit()
            render = create_render(
                db,
                project_id,
                user,
                RenderIn(expected_revision=0, idempotency_key=uuid4().hex),
                settings,
            )
            render_id = render.id
        queued = []

        def dispatch(task, identifier, countdown=0):
            assert task == "video.render_video", (
                "This test must never submit speech/video-provider work"
            )
            queued.append(identifier)
            return client.send_task(
                task, args=[identifier], queue="rendering", countdown=countdown, retry=False
            )

        def no_speech(_settings):
            pytest.fail("Narration-disabled rendering must not create a TTS client")

        # Exercise the actual shared admission protocol before publishing work.
        write_signal(False)
        assert require_admission(settings) is False
        prepare_render(
            render_id,
            sessions=sessions,
            settings=settings,
            dispatcher=dispatch,
            tts_factory=no_speech,
        )
        with sessions() as db:
            render = db.get(RenderingJob, render_id)
            assert render.status == "paused" and render.error_code == "host_admission_refused"
            assert render.attempt_count == 0 and queued == []
            render.next_attempt_at = datetime.now(UTC)
            db.commit()
        write_signal(True)

        def refresh_signal():
            while not stop.wait(2):
                write_signal(True)

        heartbeat = threading.Thread(target=refresh_signal, daemon=True)
        heartbeat.start()
        assert require_admission(settings) is True
        prepare_render(
            render_id,
            sessions=sessions,
            settings=settings,
            dispatcher=dispatch,
            tts_factory=no_speech,
        )
        assert queued == [str(render_id)]
        deadline, final = time.monotonic() + 120, None
        while time.monotonic() < deadline:
            with sessions() as db:
                render = db.get(RenderingJob, render_id)
                if render.status == "succeeded":
                    final = db.get(MediaAsset, render.output_asset_id)
                    final_path = artifact_path(settings, final.object_key)
                    assert db.get(Project, project_id).status == "completed"
                    assert render.progress_percent == 100 and render.stage == "completed"
                    assert render.validation_report["integrity_decoded"] is True
                    assert final.metadata_json["archive"] == {"status": "disabled"}
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(ActualUsageCost)
                            .where(ActualUsageCost.project_id == project_id)
                        )
                        == 0
                    )
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(GenerationAttempt)
                            .join(GenerationJob)
                            .where(GenerationJob.project_id == project_id)
                        )
                        == 0
                    )
                    break
                if render.status in {"failed", "cancelled", "paused"}:
                    pytest.fail(
                        f"Synthetic queue render did not complete: {render.status}/{render.error_code}"
                    )
            time.sleep(0.2)
        else:
            pytest.fail(
                "Actual rendering queue did not complete synthetic 2-second video within 120 seconds"
            )
        assert final is not None and final.kind == "final_video"
        verified = validate_output(final_path, 2, 1920, 1080)
        assert verified.video_codec == "h264" and verified.audio_codec == "aac"
        assert probe_media(final_path).fps == 30
        assert hashlib.sha256(final_path.read_bytes()).hexdigest() == final.sha256
    finally:
        stop.set()
        if heartbeat:
            heartbeat.join(timeout=3)
        # Cancel an unfinished fixture before removing its inputs. The lease
        # callback stops only this test's renderer; no worker/service is restarted.
        with sessions() as db:
            render = db.get(RenderingJob, render_id) if render_id else None
            unfinished = render and render.status in {"admitted", "running"}
            if unfinished:
                render.status, render.lease_token = "cancelled", None
                db.commit()
        if unfinished:
            time.sleep(4)
        with sessions() as db:
            versions = select(SceneVersion.id).join(Scene).where(Scene.project_id == project_id)
            jobs = select(GenerationJob.id).where(GenerationJob.project_id == project_id)
            db.execute(delete(AuditEvent).where(AuditEvent.project_id == project_id))
            db.execute(delete(ActualUsageCost).where(ActualUsageCost.project_id == project_id))
            db.execute(delete(GenerationAttempt).where(GenerationAttempt.job_id.in_(jobs)))
            db.execute(delete(RenderingJob).where(RenderingJob.project_id == project_id))
            db.execute(delete(GenerationJob).where(GenerationJob.project_id == project_id))
            db.execute(delete(MediaAsset).where(MediaAsset.project_id == project_id))
            db.execute(delete(SceneVersion).where(SceneVersion.id.in_(versions)))
            db.execute(delete(Scene).where(Scene.project_id == project_id))
            db.execute(delete(ProjectSettings).where(ProjectSettings.project_id == project_id))
            db.execute(delete(Project).where(Project.id == project_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
        client.close()
        engine.dispose()
        SIGNAL.unlink(missing_ok=True)
        if folder.resolve() == folder and folder.parent == SHARED:
            shutil.rmtree(folder)
