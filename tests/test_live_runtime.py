"""Disposable Docker integration. Never point these tests at the VPS."""

import os
import secrets
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
import pytest
from app.db import make_engine, make_session_factory
from app.models import AuditEvent, AuthSession, Project, ProjectSettings, User
from app.security import hash_password
from sqlalchemy import delete, select


@pytest.mark.integration
def test_real_database_authentication_and_project_roundtrip():
    base = os.environ.get("VIDEO_TEST_API_URL")
    url = os.environ.get("VIDEO_LIVE_DATABASE_URL")
    if not base or not url:
        pytest.skip("Set isolated VIDEO_TEST_API_URL and VIDEO_LIVE_DATABASE_URL")
    assert urlsplit(base).hostname == "api", "Only disposable Compose service target is allowed"
    assert urlsplit(url).hostname == "postgres"
    assert urlsplit(url).path == "/video_studio"
    engine = make_engine(url)
    sessions = make_session_factory(engine)
    email = f"ci-{secrets.token_hex(8)}@example.com"
    password = secrets.token_urlsafe(24)
    with sessions() as db:
        user = User(email=email, role="editor", password_hash=hash_password(password))
        db.add(user)
        db.commit()
        user_id = user.id
    try:
        with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
            assert client.get("/health/ready").status_code == 200
            assert client.get("/api/v1/projects").status_code == 401
            response = client.post(
                "/api/v1/auth/login", json={"email": email, "password": password}
            )
            assert response.status_code == 200, response.text
            assert "httponly" in response.headers["set-cookie"].lower()
            response = client.post(
                "/api/v1/projects",
                headers={"Origin": "http://localhost:8100"},
                json={"name": "Disposable Phase 1 verification", "budget_usd": "1.0000"},
            )
            assert response.status_code == 201, response.text
            project_id = response.json()["id"]
            assert client.get(f"/api/v1/projects/{project_id}").status_code == 200
            assert (
                client.post(
                    "/api/v1/auth/logout", headers={"Origin": "http://localhost:8100"}
                ).status_code
                == 204
            )
            assert client.get("/api/v1/projects").status_code == 401
    finally:
        with sessions() as db:
            ids = select(Project.id).where(Project.owner_id == user_id)
            db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == user_id))
            db.execute(delete(ProjectSettings).where(ProjectSettings.project_id.in_(ids)))
            db.execute(delete(Project).where(Project.owner_id == user_id))
            db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
        engine.dispose()


@pytest.mark.integration
def test_frontend_same_origin_proxy_catalog_and_paid_gate():
    base = os.environ.get("VIDEO_TEST_FRONTEND_URL")
    url = os.environ.get("VIDEO_LIVE_DATABASE_URL")
    if not base or not url:
        pytest.skip("Set isolated frontend proxy and database URLs")
    assert urlsplit(base).hostname == "frontend"
    assert urlsplit(url).hostname == "postgres" and urlsplit(url).path == "/video_studio"
    engine = make_engine(url)
    sessions = make_session_factory(engine)
    email, password = f"proxy-{secrets.token_hex(8)}@example.com", secrets.token_urlsafe(24)
    with sessions() as db:
        user = User(email=email, role="editor", password_hash=hash_password(password))
        db.add(user)
        db.commit()
        user_id = user.id
    try:
        with httpx.Client(base_url=base, timeout=15, trust_env=False) as client:
            page = client.get("/")
            assert page.status_code == 200 and "MurrCloud" in page.text
            response = client.post(
                "/api/v1/auth/login", json={"email": email, "password": password}
            )
            assert response.status_code == 200, response.text
            models = client.get("/api/v1/models").json()
            assert len(models) == 4 and sum(model["enabled"] for model in models) == 2
            config = client.get("/api/v1/generation-config").json()
            assert config["paid_generation_enabled"] is False
            headers = {"Origin": "http://localhost:3100"}
            project = client.post(
                "/api/v1/projects",
                headers=headers,
                json={"name": "Disposable selector verification", "budget_usd": "2"},
            )
            assert project.status_code == 201, project.text
            project_id = project.json()["id"]
            estimate = client.post(
                f"/api/v1/projects/{project_id}/generations/estimate",
                headers=headers,
                json={
                    "model_id": "fal-ai/wan/v2.7/text-to-video",
                    "prompt": "A calm ocean at sunrise",
                    "duration_seconds": 5,
                    "mode": "text_to_video",
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                    "audio_enabled": True,
                },
            )
            assert estimate.status_code == 201, estimate.text
            assert estimate.json()["pricing_verified"] is False
            blocked = client.post(
                f"/api/v1/projects/{project_id}/generations",
                headers=headers,
                json={
                    "estimate_id": estimate.json()["id"],
                    "idempotency_key": secrets.token_hex(16),
                    "approved_cap_usd": "2",
                    "approve_unverified_price": True,
                },
            )
            assert (
                blocked.status_code == 409
                and blocked.json()["detail"] == "paid_generation_disabled"
            )
            assert client.get(f"/api/v1/projects/{project_id}/generations").json() == []
            assert (
                Decimal(client.get(f"/api/v1/projects/{project_id}").json()["committed_cost_usd"])
                == 0
            )
    finally:
        from app.models import CostEstimate

        with sessions() as db:
            ids = select(Project.id).where(Project.owner_id == user_id)
            db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == user_id))
            db.execute(delete(CostEstimate).where(CostEstimate.project_id.in_(ids)))
            db.execute(delete(ProjectSettings).where(ProjectSettings.project_id.in_(ids)))
            db.execute(delete(Project).where(Project.owner_id == user_id))
            db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
        engine.dispose()
