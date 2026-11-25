"""Phase 3 roundtrip against the disposable Compose API and Next.js proxy only."""

import io
import os
import secrets
import zipfile
from urllib.parse import urlsplit

import httpx
import pytest
from app.db import make_engine, make_session_factory
from app.ingestion import DOCX_MIME
from app.models import (
    AuditEvent,
    AuthSession,
    CostEstimate,
    MediaAsset,
    Project,
    ProjectInput,
    ProjectSettings,
    Scene,
    SceneReferenceAsset,
    SceneVersion,
    Storyboard,
    StoryboardEstimate,
    StoryboardRevision,
    User,
)
from app.security import hash_password
from PIL import Image
from sqlalchemy import delete, select


@pytest.mark.integration
@pytest.mark.parametrize("surface", ["api", "frontend"])
def test_live_manual_planning_imports_validation_and_disabled_director(surface):
    base = os.environ.get("VIDEO_TEST_API_URL" if surface == "api" else "VIDEO_TEST_FRONTEND_URL")
    url = os.environ.get("VIDEO_LIVE_DATABASE_URL")
    if not base or not url:
        pytest.skip("Isolated Compose planning surface is not available")
    assert urlsplit(base).hostname == surface
    assert urlsplit(url).hostname == "postgres" and urlsplit(url).path == "/video_studio"
    engine = make_engine(url)
    sessions = make_session_factory(engine)
    email, password = f"planning-{secrets.token_hex(8)}@example.com", secrets.token_urlsafe(24)
    with sessions() as db:
        user = User(email=email, role="editor", password_hash=hash_password(password))
        db.add(user)
        db.commit()
        user_id = user.id
    origin = "http://localhost:8100" if surface == "api" else "http://localhost:3100"
    headers = {"Origin": origin}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        package.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>测试中文脚本。An English narration.</w:t></w:r></w:p></w:body></w:document>',
        )
    image = io.BytesIO()
    Image.new("RGB", (320, 300), "orange").save(image, "PNG")
    try:
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            assert (
                client.post(
                    "/api/v1/auth/login", json={"email": email, "password": password}
                ).status_code
                == 200
            )
            assert client.get("/api/v1/director-config").json()["director_enabled"] is False
            created = client.post(
                "/api/v1/projects",
                headers=headers,
                json={
                    "name": "Disposable Phase 3 plan",
                    "target_duration_seconds": 10,
                    "budget_usd": "5",
                },
            )
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]
            path = f"/api/v1/projects/{project_id}"
            imported = client.post(
                path + "/imports/docx",
                headers=headers,
                files={"file": ("script.docx", archive.getvalue(), DOCX_MIME)},
            )
            assert imported.status_code == 201, imported.text
            uploaded = client.post(
                path + "/assets/reference-images",
                headers=headers,
                files={"file": ("ref.png", image.getvalue(), "image/png")},
                data={"caption": "规划参考"},
            )
            assert uploaded.status_code == 201, uploaded.text
            reference = uploaded.json()
            preview = client.get(reference["preview_url"])
            assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
            board = client.get(path + "/storyboard").json()
            assert board["revision"] == 1 and board["input_id"] == imported.json()["id"]
            scenes = [
                {
                    "scene_id": f"scene_{i:03d}",
                    "order": i,
                    "duration_seconds": 5,
                    "generation_mode": "text_to_video",
                    "video_model_id": "fal-ai/wan/v2.7/text-to-video",
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                    "audio_enabled": True,
                    "visual_prompt": "A calm ocean",
                    "negative_prompt": "",
                    "narration": "中文旁白。",
                    "reference_asset_ids": [reference["id"]],
                    "transition": "fade",
                    "status": "draft",
                }
                for i in (1, 2)
            ]
            saved = client.put(
                path + "/storyboard",
                headers=headers,
                json={
                    "expected_revision": board["revision"],
                    "title": "中文故事板",
                    "target_duration_seconds": 10,
                    "language": "zh-CN",
                    "visual_style": "cinematic",
                    "target_audience": "Internal team",
                    "default_model_id": "fal-ai/wan/v2.7/text-to-video",
                    "scenes": scenes,
                },
            )
            assert saved.status_code == 200, saved.text
            revision = saved.json()["revision"]
            validated = client.post(
                path + "/storyboard/validate", headers=headers, json={"expected_revision": revision}
            )
            assert validated.status_code == 200 and validated.json()["valid"], validated.text
            estimated = client.post(
                path + "/storyboard/estimate",
                headers=headers,
                json={"expected_revision": revision, "retry_allowance_percent": "0"},
            )
            assert estimated.status_code == 201 and len(estimated.json()["scenes"]) == 2, (
                estimated.text
            )
            approval = client.post(
                path + "/storyboard/approve",
                headers=headers,
                json={
                    "estimate_id": estimated.json()["id"],
                    "expected_revision": revision,
                    "idempotency_key": secrets.token_hex(16),
                    "approved_cap_usd": "5",
                    "approve_unverified_price": True,
                },
            )
            assert (
                approval.status_code == 409
                and approval.json()["detail"] == "paid_generation_disabled"
            )
            director = client.post(
                path + "/director/jobs",
                headers=headers,
                json={
                    "input_id": imported.json()["id"],
                    "expected_revision": revision,
                    "idempotency_key": secrets.token_hex(16),
                    "max_cost_usd": "0.10",
                    "approved_cap_usd": "5",
                    "approve_unknown_price": True,
                },
            )
            assert (
                director.status_code == 409 and director.json()["detail"] == "director_disabled"
            ), director.text
            assert client.get(path + "/director/jobs").json() == []
            assert client.get(path + "/generations").json() == []
    finally:
        with sessions() as db:
            ids = select(Project.id).where(Project.owner_id == user_id)
            scenes = select(Scene.id).where(Scene.project_id.in_(ids))
            versions = select(SceneVersion.id).where(SceneVersion.scene_id.in_(scenes))
            boards = select(Storyboard.id).where(Storyboard.project_id.in_(ids))
            db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == user_id))
            db.execute(delete(StoryboardEstimate).where(StoryboardEstimate.project_id.in_(ids)))
            db.execute(delete(CostEstimate).where(CostEstimate.project_id.in_(ids)))
            db.execute(
                delete(StoryboardRevision).where(StoryboardRevision.storyboard_id.in_(boards))
            )
            db.execute(delete(Storyboard).where(Storyboard.project_id.in_(ids)))
            db.execute(delete(ProjectInput).where(ProjectInput.project_id.in_(ids)))
            db.execute(
                delete(SceneReferenceAsset).where(
                    SceneReferenceAsset.scene_version_id.in_(versions)
                )
            )
            db.execute(delete(SceneVersion).where(SceneVersion.scene_id.in_(scenes)))
            db.execute(delete(Scene).where(Scene.project_id.in_(ids)))
            db.execute(delete(MediaAsset).where(MediaAsset.project_id.in_(ids)))
            db.execute(delete(ProjectSettings).where(ProjectSettings.project_id.in_(ids)))
            db.execute(delete(Project).where(Project.owner_id == user_id))
            db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
        engine.dispose()
