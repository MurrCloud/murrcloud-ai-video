"""Private production video/SRT delivery through disposable API and Next.js proxy.

Synthetic fixture transport verification, not paid generation or browser visual QA.
"""

import hashlib
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from app.db import make_engine, make_session_factory
from app.generation import fingerprint
from app.models import (
    AuditEvent,
    AuthSession,
    MediaAsset,
    Project,
    ProjectSettings,
    RenderingJob,
    User,
)
from app.production import source_binding
from app.production_schemas import ProductionSettings
from app.production_storage import artifact_key
from app.security import hash_password
from sqlalchemy import delete

SHARED = Path("/var/lib/video")


@pytest.mark.integration
@pytest.mark.parametrize("surface", ["api", "frontend"])
def test_private_production_range_preview_mp4_and_utf8_srt_delivery(surface):
    base = os.environ.get("VIDEO_TEST_API_URL" if surface == "api" else "VIDEO_TEST_FRONTEND_URL")
    url = os.environ.get("VIDEO_LIVE_DATABASE_URL")
    if not base or not url:
        pytest.skip("Isolated Compose production delivery surface is not available")
    assert urlsplit(base).hostname == surface
    assert urlsplit(url).hostname == "postgres" and urlsplit(url).path == "/video_studio"
    assert SHARED.resolve() == SHARED and SHARED.is_dir()
    engine = make_engine(url)
    sessions = make_session_factory(engine)
    project_id, owner_id, other_id, output_id, subtitle_id, render_id = [uuid4() for _ in range(6)]
    folder = SHARED / str(project_id)
    production_folder = folder / "productions"
    production_folder.mkdir(parents=True, mode=0o700)
    owner_email, other_email = (
        f"delivery-{uuid4().hex}@example.com",
        f"other-{uuid4().hex}@example.com",
    )
    password = secrets.token_urlsafe(24)
    video_key, subtitle_key = (
        artifact_key(project_id, output_id, "mp4"),
        artifact_key(project_id, subtitle_id, "srt"),
    )
    video, subtitles = SHARED / video_key, SHARED / subtitle_key
    subtitle_body = "1\n00:00:00,000 --> 00:00:00,400\nMurrCloud 字幕下载验证。\n"
    origin = "http://localhost:8100" if surface == "api" else "http://localhost:3100"
    headers = {"Origin": origin}
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
                "color=c=0xFF7D00:s=320x180:r=30:d=0.5",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t",
                "0.5",
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
                str(video),
            ],
            check=True,
            timeout=15,
            capture_output=True,
        )
        subtitles.write_text(subtitle_body, encoding="utf-8")
        os.chmod(video, 0o600)
        os.chmod(subtitles, 0o600)
        video_bytes = video.read_bytes()
        video_digest = hashlib.sha256(video_bytes).hexdigest()
        with sessions() as db:
            owner = User(
                id=owner_id, email=owner_email, role="editor", password_hash=hash_password(password)
            )
            other = User(
                id=other_id, email=other_email, role="editor", password_hash=hash_password(password)
            )
            db.add_all([owner, other])
            db.flush()
            project = Project(
                id=project_id,
                owner_id=owner_id,
                name="Synthetic delivery fixture",
                status="completed",
                target_duration_seconds=1,
                budget_usd=0,
            )
            db.add(project)
            db.flush()
            options = ProductionSettings(narration_enabled=False, burn_subtitles=False)
            db.add(
                ProjectSettings(
                    project_id=project_id,
                    narration_enabled=False,
                    composition={"production": options.model_dump(mode="json")},
                )
            )
            db.add_all(
                [
                    MediaAsset(
                        id=output_id,
                        project_id=project_id,
                        kind="final_video",
                        object_key=video_key,
                        mime_type="video/mp4",
                        size_bytes=len(video_bytes),
                        sha256=video_digest,
                        status="ready",
                        metadata_json={
                            "fixture": "synthetic transport only",
                            "width": 320,
                            "height": 180,
                        },
                    ),
                    MediaAsset(
                        id=subtitle_id,
                        project_id=project_id,
                        kind="subtitles",
                        object_key=subtitle_key,
                        mime_type="application/x-subrip",
                        size_bytes=subtitles.stat().st_size,
                        sha256=hashlib.sha256(subtitles.read_bytes()).hexdigest(),
                        status="ready",
                    ),
                ]
            )
            db.flush()
            # Delivery history only: actual worker completion is independently
            # verified by test_production_worker_runtime, never inferred here.
            db.add(
                RenderingJob(
                    id=render_id,
                    project_id=project_id,
                    requested_by_id=owner_id,
                    idempotency_key=uuid4().hex,
                    status="succeeded",
                    stage="completed",
                    progress_percent=100,
                    output_asset_id=output_id,
                    subtitle_asset_id=subtitle_id,
                    composition_snapshot={"binding_hash": fingerprint(source_binding(db, project))},
                    validation_report={"synthetic_transport_fixture": True},
                )
            )
            db.commit()
        preview_url = f"/api/v1/assets/{output_id}/preview"
        download_url = f"/api/v1/assets/{output_id}/download"
        srt_url = f"/api/v1/assets/{subtitle_id}/download"
        with httpx.Client(base_url=base, timeout=20, trust_env=False) as client:
            for path in (preview_url, download_url, srt_url, "/api/v1/production-config"):
                assert client.get(path).status_code == 401
            login = client.post(
                "/api/v1/auth/login",
                headers=headers,
                json={"email": owner_email, "password": password},
            )
            assert login.status_code == 200, login.text
            config = client.get("/api/v1/production-config")
            assert config.status_code == 200
            assert config.json()["render_enabled"] is False
            assert config.json()["narration_enabled"] is False
            history = client.get(f"/api/v1/projects/{project_id}/production")
            assert history.status_code == 200, history.text
            render = next(
                row for row in history.json()["render_jobs"] if row["id"] == str(render_id)
            )
            assert render["preview_url"] == preview_url and render["download_url"] == download_url
            assert render["srt_url"] == srt_url
            ranged = client.get(preview_url, headers={"Range": "bytes=0-31"})
            assert ranged.status_code == 206, ranged.text
            assert ranged.content == video_bytes[:32] and len(ranged.content) == 32
            assert ranged.headers["content-range"] == f"bytes 0-31/{len(video_bytes)}"
            assert ranged.headers["accept-ranges"] == "bytes"
            assert ranged.headers["content-type"] == "video/mp4"
            assert "inline" in ranged.headers["content-disposition"]
            full = client.get(download_url)
            assert full.status_code == 200 and full.content[4:8] == b"ftyp"
            assert hashlib.sha256(full.content).hexdigest() == video_digest
            assert "attachment" in full.headers["content-disposition"]
            srt = client.get(srt_url)
            assert srt.status_code == 200 and srt.content == subtitle_body.encode("utf-8")
            assert srt.content.decode("utf-8") == subtitle_body
            assert srt.headers["content-type"].split(";", 1)[0] == "application/x-subrip"
            other_login = client.post(
                "/api/v1/auth/login",
                headers=headers,
                json={"email": other_email, "password": password},
            )
            assert other_login.status_code == 200, other_login.text
            assert client.get("/api/v1/auth/me").json()["id"] == str(other_id)
            for path in (
                preview_url,
                download_url,
                srt_url,
                f"/api/v1/projects/{project_id}/production",
            ):
                assert client.get(path).status_code == 404
    finally:
        with sessions() as db:
            db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id.in_([owner_id, other_id])))
            db.execute(delete(RenderingJob).where(RenderingJob.project_id == project_id))
            db.execute(delete(MediaAsset).where(MediaAsset.project_id == project_id))
            db.execute(delete(ProjectSettings).where(ProjectSettings.project_id == project_id))
            db.execute(delete(Project).where(Project.id == project_id))
            db.execute(delete(AuthSession).where(AuthSession.user_id.in_([owner_id, other_id])))
            db.execute(delete(User).where(User.id.in_([owner_id, other_id])))
            db.commit()
        engine.dispose()
        if folder.resolve() == folder and folder.parent == SHARED:
            shutil.rmtree(folder)
