"""Account revocation, operator authorization and bounded evidence without network."""

from datetime import UTC, datetime, timedelta

import pytest
from app.models import AuditEvent, AuthSession, WorkerHeartbeat
from app.monitoring import storage_evidence
from app.security import SESSION_COOKIE
from app.telemetry import record_heartbeat
from conftest import ORIGIN, PASSWORD
from sqlalchemy import func, select

HEADERS = {"Origin": ORIGIN}
NEW_PASSWORD = "changed-offline-password-456!"


def test_settings_requires_auth_and_never_exposes_secret_configuration(api, login):
    assert api.client.get("/api/v1/settings").status_code == 401
    login("viewer")
    body = api.client.get("/api/v1/settings").json()
    assert body["user"]["role"] == "viewer"
    assert body["limits"]["render_concurrency"] == body["limits"]["ffmpeg_threads"] == 1
    assert not any(body["features"].values())
    assert body["providers"]["fal_configured"] is False
    text = api.client.get("/api/v1/settings").text
    assert api.settings.session_secret.get_secret_value() not in text
    for private in (
        "database_url",
        "redis_url",
        "approval_reference",
        "password_hash",
        "s3_endpoint",
        "s3_bucket",
    ):
        assert private not in text


def test_password_change_revokes_all_sessions_and_requires_new_login(api, login):
    login()
    first_token = api.client.cookies.get(SESSION_COOKIE)
    login()
    response = api.client.post(
        "/api/v1/account/password",
        json={
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        headers=HEADERS,
    )
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert api.client.get("/api/v1/settings").status_code == 401
    api.client.cookies.set(SESSION_COOKIE, first_token)
    assert api.client.get("/api/v1/settings").status_code == 401
    api.client.cookies.clear()
    assert (
        api.client.post(
            "/api/v1/auth/login",
            json={"email": "editor@example.com", "password": PASSWORD},
            headers=HEADERS,
        ).status_code
        == 401
    )
    assert (
        api.client.post(
            "/api/v1/auth/login",
            json={"email": "editor@example.com", "password": NEW_PASSWORD},
            headers=HEADERS,
        ).status_code
        == 200
    )
    with api.sessions() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuthSession)
                .where(
                    AuthSession.user_id == api.user_ids["editor"],
                    AuthSession.revoked_at.is_not(None),
                )
            )
            == 2
        )
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "auth.password_changed")
        ).all()
        assert len(events) == 1 and events[0].details == {}


@pytest.mark.parametrize(
    "change,status",
    [
        ({"current_password": "incorrect"}, 403),
        ({"confirm_password": "mismatch-password-12"}, 422),
        ({"new_password": "short", "confirm_password": "short"}, 422),
        ({"new_password": PASSWORD, "confirm_password": PASSWORD}, 422),
        ({"unexpected": "secret"}, 422),
    ],
)
def test_password_failures_preserve_account_and_redact_inputs(api, login, change, status):
    login()
    payload = {
        "current_password": PASSWORD,
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
        **change,
    }
    response = api.client.post("/api/v1/account/password", json=payload, headers=HEADERS)
    assert response.status_code == status
    assert PASSWORD not in response.text and NEW_PASSWORD not in response.text
    assert api.client.get("/api/v1/auth/me").status_code == 200
    with api.sessions() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuthSession)
                .where(AuthSession.revoked_at.is_not(None))
            )
            == 0
        )


def test_password_origin_and_rate_limits_fail_closed(api, login):
    login()
    payload = {
        "current_password": "wrong",
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }
    assert api.client.post("/api/v1/account/password", json=payload).status_code == 403
    for _ in range(5):
        assert (
            api.client.post("/api/v1/account/password", json=payload, headers=HEADERS).status_code
            == 403
        )
    assert (
        api.client.post("/api/v1/account/password", json=payload, headers=HEADERS).status_code
        == 429
    )
    api.limiter.unavailable = True
    assert (
        api.client.post("/api/v1/account/password", json=payload, headers=HEADERS).status_code
        == 503
    )


@pytest.mark.parametrize("role", ["viewer", "editor", "other"])
def test_global_operations_and_billing_require_admin(api, login, role):
    login(role)
    for path in (
        "/operations",
        "/operations/audit",
        "/operations/reconciliations",
        "/operations/costs",
    ):
        assert api.client.get("/api/v1" + path).status_code == 403
    for path in (
        "/operations/reconciliations/generation/00000000-0000-0000-0000-000000000000",
        "/operations/costs/00000000-0000-0000-0000-000000000000/confirm",
    ):
        assert api.client.post("/api/v1" + path, json={}, headers=HEADERS).status_code == 403


def test_admin_operations_exposes_safe_metadata_and_queue_basis(api, login, tmp_path):
    api.app.state.settings = api.settings.model_copy(
        update={"storage_dir": tmp_path, "render_enabled": True}
    )
    with api.sessions() as db:
        db.add(
            AuditEvent(
                event_type="test.private_evidence",
                details={"prompt": "PRIVATE CUSTOMER PROMPT", "key": "do-not-expose"},
            )
        )
        db.commit()
    login("admin")
    response = api.client.get("/api/v1/operations")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "attention" and body["admission"]["allowed"] is False
    assert body["storage"]["available"] is True and body["storage"]["used_bytes"] == 0
    assert all(queue["basis"] == "persisted_job_states" for queue in body["queues"])
    assert "rendering_worker_missing" in {a["code"] for a in body["alerts"]}
    events = api.client.get("/api/v1/operations/audit?limit=1").json()
    assert len(events["items"]) == 1 and events["total"] >= 2
    text = api.client.get("/api/v1/operations/audit").text
    assert "PRIVATE CUSTOMER PROMPT" not in text and "do-not-expose" not in text
    assert api.client.get("/api/v1/operations/audit?limit=101").status_code == 422


def test_storage_scan_refuses_links_and_incomplete_evidence(api, tmp_path):
    config = api.settings.model_copy(update={"storage_dir": tmp_path})
    (tmp_path / "clip.mp4").write_bytes(b"x" * 10)
    assert storage_evidence(config)["used_bytes"] == 10
    assert storage_evidence(config, max_entries=0)["used_bytes"] is None
    (tmp_path / "link").symlink_to("/etc/passwd")
    assert storage_evidence(config)["available"] is False
    assert storage_evidence(config)["used_bytes"] is None


def test_worker_heartbeat_updates_and_prunes_only_old_instrumentation(api):
    now = datetime.now(UTC)
    with api.sessions() as db:
        assert record_heartbeat(
            db, "video-rendering@test", "rendering", now=now - timedelta(days=8)
        )
        assert record_heartbeat(db, "video-generation@test", "generation", now=now)
        assert db.scalar(select(func.count()).select_from(WorkerHeartbeat)) == 1
        row = db.scalar(select(WorkerHeartbeat))
        identifier = row.id
        assert record_heartbeat(
            db, "video-generation@test", "generation", now=now + timedelta(seconds=30)
        )
        assert db.scalar(select(WorkerHeartbeat.id)) == identifier
        assert not record_heartbeat(db, "unknown", "erp", now=now)
        assert db.scalar(select(func.count()).select_from(WorkerHeartbeat)) == 1
