"""Synthetic Phase 5 API/proxy acceptance in disposable Compose, never ERP load.

Creates only random fixture users/projects. No provider, paid generation or render
endpoint is called. The bounded read burst is twelve GETs at concurrency four.
"""

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import pytest
from app.db import make_engine, make_session_factory
from app.library_schemas import PortablePlan, PortableSettings
from app.models import (
    ActualUsageCost,
    AuditEvent,
    AuthSession,
    CostEstimate,
    DirectorJob,
    GenerationJob,
    MediaAsset,
    Project,
    ProjectInput,
    ProjectSettings,
    ProjectTemplate,
    RenderingJob,
    Scene,
    SceneReferenceAsset,
    SceneVersion,
    Storyboard,
    StoryboardEstimate,
    StoryboardRevision,
    User,
    WorkspaceMutation,
)
from app.security import hash_password
from sqlalchemy import delete, func, or_, select
from sqlalchemy.engine import make_url


def isolated_surface(surface):
    base = os.environ.get("VIDEO_TEST_API_URL" if surface == "api" else "VIDEO_TEST_FRONTEND_URL")
    url = os.environ.get("VIDEO_LIVE_DATABASE_URL")
    if not base or not url:
        pytest.skip("Isolated Compose workspace surface is not available")
    parsed, database = urlsplit(base), make_url(url)
    port = 8100 if surface == "api" else 3100
    assert parsed.scheme == "http" and parsed.netloc == f"{surface}:{port}"
    assert parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
    assert database.drivername == "postgresql+psycopg" and database.host == "postgres"
    assert database.port in {None, 5432} and database.database == "video_studio"
    assert not database.query, "Disposable Compose database URL only"
    return base, url


def ok(response, status=200):
    assert response.status_code == status, (
        f"{response.request.method} {response.request.url.path}: {response.status_code}"
    )
    return response.json() if status != 204 else None


def field_names(value):
    if isinstance(value, dict):
        return set(value) | {name for child in value.values() for name in field_names(child)}
    if isinstance(value, list):
        return {name for child in value for name in field_names(child)}
    return set()


@pytest.mark.integration
@pytest.mark.parametrize("surface", ["api", "frontend"])
def test_live_workspace_pagination_reuse_security_and_bounded_reads(surface):
    base, url = isolated_surface(surface)
    engine = make_engine(url)
    sessions = make_session_factory(engine)
    marker = "workspace-" + secrets.token_hex(8)
    password, replacement = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    emails = {role: f"{marker}-{role}@example.com" for role in ("editor", "admin")}
    users, project_ids = {}, []
    with sessions() as db:
        for role, email in emails.items():
            user = User(email=email, role=role, password_hash=hash_password(password))
            db.add(user)
            db.flush()
            users[role] = user.id
        for index in range(60):
            status = (
                "draft"
                if index < 40
                else "completed"
                if index < 50
                else "failed"
                if index < 55
                else "generating"
            )
            project = Project(
                owner_id=users["editor"],
                name=f"{marker}-{index:03d}",
                prompt="Private synthetic workspace brief",
                status=status,
                budget_usd=Decimal("10"),
                approved_cap_usd=Decimal("3"),
                committed_cost_usd=Decimal("1"),
            )
            db.add(project)
            db.flush()
            project_ids.append(project.id)
            db.add(
                ProjectSettings(
                    project_id=project.id,
                    resolution="720p",
                    default_model_id="fal-ai/wan/v2.7/text-to-video",
                )
            )
        foreign = Project(
            owner_id=users["admin"], name=marker + "-private-other", budget_usd=Decimal("999")
        )
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
        db.add(ProjectSettings(project_id=foreign_id))
        db.add(
            ProjectTemplate(
                owner_id=users["admin"],
                name=marker + "-private-template",
                production_plan=PortablePlan(
                    target_duration_seconds=15, settings=PortableSettings()
                ).model_dump(mode="json"),
            )
        )
        db.add(
            Scene(
                project_id=project_ids[0],
                scene_key="scene_001",
                order_index=1,
                duration_seconds=5,
                video_model_id="fal-ai/wan/v2.7/text-to-video",
                visual_prompt="Private original scene fixture",
                generation_settings={
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                    "audio_enabled": True,
                },
            )
        )
        for project_id, amount, status in (
            (project_ids[0], Decimal("1.25"), "confirmed"),
            (project_ids[1], None, "unavailable"),
            (project_ids[2], None, "pending"),
        ):
            db.add(
                ActualUsageCost(
                    project_id=project_id,
                    provider="offline-workspace-fixture",
                    amount_usd=amount,
                    status=status,
                )
            )
        db.commit()
    origin = "http://localhost:8100" if surface == "api" else "http://localhost:3100"
    headers = {"Origin": origin}
    try:
        with (
            httpx.Client(base_url=base, timeout=30, trust_env=False) as client,
            httpx.Client(base_url=base, timeout=30, trust_env=False) as admin,
        ):
            ok(
                client.post(
                    "/api/v1/auth/login", json={"email": emails["editor"], "password": password}
                )
            )
            seen = set()
            for offset, count in ((0, 24), (24, 24), (48, 12)):
                page = ok(
                    client.get(
                        "/api/v1/library/projects",
                        params={"q": marker, "limit": 24, "offset": offset},
                    )
                )
                assert page["total"] == 60 and page["offset"] == offset
                assert len(page["items"]) == count
                current = {UUID(row["id"]) for row in page["items"]}
                assert not seen & current
                assert all(UUID(row["owner_id"]) == users["editor"] for row in page["items"])
                seen |= current
            assert seen == set(project_ids)
            dashboard = ok(client.get("/api/v1/dashboard"))
            assert {
                key: dashboard[key]
                for key in ("projects_total", "completed", "failed", "in_production")
            } == {"projects_total": 60, "completed": 10, "failed": 5, "in_production": 5}
            assert len(dashboard["recent_projects"]) == 8
            assert all(UUID(row["id"]) in seen for row in dashboard["recent_projects"])
            assert Decimal(dashboard["costs"]["committed_usd"]) == 60
            report = ok(client.get("/api/v1/reports/usage", params={"limit": 1}))
            assert report["scope"] == "owned" and report["total"] == 3 and len(report["items"]) == 1
            totals = report["totals"]
            assert totals["project_count"] == 60 and totals["job_count"] == 0
            assert (
                Decimal(totals["budget_usd"]) == 600 and Decimal(totals["approved_cap_usd"]) == 180
            )
            assert (
                Decimal(totals["committed_usd"]) == 60
                and Decimal(totals["exposure_usd"]) == Decimal("61.25")
                and Decimal(totals["remaining_budget_usd"]) == Decimal("538.75")
                and Decimal(totals["remaining_approved_usd"]) == Decimal("118.75")
            )
            assert Decimal(totals["confirmed_usd"]) == Decimal("1.25")
            assert (
                totals["unknown_cost_count"] == totals["pending_cost_count"] == 1
                and totals["total_actual_usd"] is None
            )
            assert (
                client.get(
                    "/api/v1/reports/usage", params={"project_id": str(foreign_id)}
                ).status_code
                == 404
            )
            config = ok(client.get("/api/v1/settings"))
            assert config["user"]["id"] == str(users["editor"])
            assert config["features"]["paid_generation_enabled"] is False
            assert config["limits"]["render_concurrency"] == config["limits"]["ffmpeg_threads"] == 1
            assert not field_names(config) & {
                "database_url",
                "redis_url",
                "session_secret",
                "fal_key",
                "openai_key",
                "s3_secret_key",
                "password_hash",
                "token_hash",
            }
            assert client.get("/api/v1/operations").status_code == 403

            source = f"/api/v1/projects/{project_ids[0]}"
            payload = {"name": marker + " copy", "idempotency_key": secrets.token_hex(16)}
            copied = ok(client.post(source + "/duplicate", headers=headers, json=payload), 201)
            assert (
                ok(client.post(source + "/duplicate", headers=headers, json=payload), 201)["id"]
                == copied["id"]
            )
            assert copied["approved_cap_usd"] is None and Decimal(copied["committed_cost_usd"]) == 0
            saved = ok(
                client.post(
                    source + "/templates",
                    headers=headers,
                    json={"name": marker + " template", "idempotency_key": secrets.token_hex(16)},
                ),
                201,
            )
            assert saved["scene_count"] == 1 and not saved["shared"]
            template_route = f"/api/v1/templates/{saved['id']}"
            payload = {
                "name": marker + " applied",
                "budget_usd": "2",
                "idempotency_key": secrets.token_hex(16),
            }
            applied = ok(client.post(template_route + "/apply", headers=headers, json=payload), 201)
            assert (
                ok(client.post(template_route + "/apply", headers=headers, json=payload), 201)["id"]
                == applied["id"]
            )
            assert (
                applied["approved_cap_usd"] is None and Decimal(applied["committed_cost_usd"]) == 0
            )
            archived = ok(
                client.post(
                    f"/api/v1/projects/{applied['id']}/archive",
                    headers=headers,
                    json={"expected_updated_at": applied["updated_at"], "archived": True},
                )
            )
            assert archived["archived"] is True
            archived_page = ok(
                client.get("/api/v1/library/projects", params={"q": marker, "archived": "true"})
            )
            assert archived_page["total"] == 1 and archived_page["items"][0]["id"] == applied["id"]
            ok(
                client.patch(
                    template_route,
                    headers=headers,
                    json={"expected_revision": saved["revision"], "archived": True},
                )
            )
            assert (
                client.post(
                    template_route + "/apply",
                    headers=headers,
                    json={**payload, "idempotency_key": secrets.token_hex(16)},
                ).status_code
                == 409
            )

            ok(
                admin.post(
                    "/api/v1/auth/login", json={"email": emails["admin"], "password": password}
                )
            )
            operations = ok(admin.get("/api/v1/operations"))
            queues = {queue["name"]: queue for queue in operations["queues"]}
            assert set(queues) == {"generation", "narration", "rendering"}
            assert all(queue["basis"] == "persisted_job_states" for queue in queues.values())
            if os.environ.get("VIDEO_TEST_RENDER_WORKER_ENABLED") == "true":
                assert queues["rendering"]["worker_count"] >= 1, (
                    "Real rendering-worker heartbeat is missing"
                )
            privileged = ok(
                admin.get(
                    "/api/v1/library/projects", params={"q": marker + "-private-other", "limit": 1}
                )
            )
            assert privileged["total"] == 1 and privileged["items"][0]["id"] == str(foreign_id)

            # Same immutable cookie, fixed twelve reads and at most four sockets.
            # This tests bounded concurrent reads, not host/ERP capacity.
            paths = [
                "/api/v1/library/projects?limit=1",
                "/api/v1/dashboard",
                "/api/v1/reports/usage?group_by=provider&limit=1",
                "/api/v1/templates?limit=1",
            ] * 3
            with ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(executor.map(client.get, paths))
            assert len(responses) == 12
            for response in responses:
                ok(response)
                assert response.headers["cache-control"] == "no-store"
            for response in responses[::4]:
                assert all(
                    row["owner_id"] == str(users["editor"]) for row in response.json()["items"]
                )
            for response in responses[3::4]:
                assert response.json()["items"] == [], "Another user's private template leaked"
            if surface == "frontend":
                page = client.get("/library")
                assert page.status_code == 200 and "MurrCloud" in page.text

            # A copied cookie proves the backend revoked the token, independently
            # of the password endpoint deleting the current browser cookie.
            with httpx.Client(
                base_url=base, cookies=httpx.Cookies(client.cookies), timeout=15, trust_env=False
            ) as stale:
                ok(
                    client.post(
                        "/api/v1/account/password",
                        headers=headers,
                        json={
                            "current_password": password,
                            "new_password": replacement,
                            "confirm_password": replacement,
                        },
                    ),
                    204,
                )
                assert stale.get("/api/v1/settings").status_code == 401
            ok(
                client.post(
                    "/api/v1/auth/login", json={"email": emails["editor"], "password": replacement}
                )
            )
            with sessions() as db:
                owned = select(Project.id).where(Project.owner_id == users["editor"])
                for model in (GenerationJob, DirectorJob, RenderingJob):
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(model)
                            .where(model.project_id.in_(owned))
                        )
                        == 0
                    )
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(Project)
                        .where(Project.owner_id == users["editor"])
                    )
                    == 62
                )
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(AuthSession)
                        .where(
                            AuthSession.user_id == users["editor"],
                            AuthSession.revoked_at.is_not(None),
                        )
                    )
                    >= 1
                )
    finally:
        with sessions() as db:
            identifiers = select(Project.id).where(Project.owner_id.in_(users.values()))
            scenes = select(Scene.id).where(Scene.project_id.in_(identifiers))
            versions = select(SceneVersion.id).where(SceneVersion.scene_id.in_(scenes))
            boards = select(Storyboard.id).where(Storyboard.project_id.in_(identifiers))
            db.execute(
                delete(WorkspaceMutation).where(WorkspaceMutation.actor_user_id.in_(users.values()))
            )
            db.execute(delete(ProjectTemplate).where(ProjectTemplate.owner_id.in_(users.values())))
            db.execute(
                delete(AuditEvent).where(
                    or_(
                        AuditEvent.actor_user_id.in_(users.values()),
                        AuditEvent.project_id.in_(identifiers),
                    )
                )
            )
            db.execute(delete(ActualUsageCost).where(ActualUsageCost.project_id.in_(identifiers)))
            db.execute(
                delete(StoryboardEstimate).where(StoryboardEstimate.project_id.in_(identifiers))
            )
            db.execute(delete(CostEstimate).where(CostEstimate.project_id.in_(identifiers)))
            db.execute(
                delete(StoryboardRevision).where(StoryboardRevision.storyboard_id.in_(boards))
            )
            db.execute(delete(Storyboard).where(Storyboard.project_id.in_(identifiers)))
            db.execute(delete(ProjectInput).where(ProjectInput.project_id.in_(identifiers)))
            db.execute(
                delete(SceneReferenceAsset).where(
                    SceneReferenceAsset.scene_version_id.in_(versions)
                )
            )
            db.execute(delete(MediaAsset).where(MediaAsset.project_id.in_(identifiers)))
            db.execute(delete(SceneVersion).where(SceneVersion.scene_id.in_(scenes)))
            db.execute(delete(Scene).where(Scene.project_id.in_(identifiers)))
            db.execute(delete(ProjectSettings).where(ProjectSettings.project_id.in_(identifiers)))
            db.execute(delete(Project).where(Project.owner_id.in_(users.values())))
            db.execute(delete(AuthSession).where(AuthSession.user_id.in_(users.values())))
            db.execute(delete(User).where(User.id.in_(users.values())))
            db.commit()
        engine.dispose()
