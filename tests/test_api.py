import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from app.models import AuditEvent, AuthSession, Project, ProjectSettings, User
from app.security import SESSION_COOKIE, hash_password, token_hash, verify_password
from conftest import ORIGIN, PASSWORD, PASSWORD_HASH
from sqlalchemy import select, text

API = "/api/v1"


def create_project(api, **overrides):
    payload = {"name": "Launch video", "prompt": "A sunrise over a calm sea", "budget_usd": "5.00"}
    payload.update(overrides)
    return api.client.post(API + "/projects", json=payload, headers={"Origin": ORIGIN})


def test_passwords_use_argon2_and_reject_malformed_hashes():
    assert PASSWORD_HASH.startswith("$argon2id$")
    assert verify_password(PASSWORD, PASSWORD_HASH)
    assert not verify_password("wrong password", PASSWORD_HASH)
    assert not verify_password(PASSWORD, "not-a-password-hash")
    with pytest.raises(ValueError):
        hash_password("short")
    with pytest.raises(ValueError):
        hash_password("a" * 257)


def test_login_sets_scoped_cookie_and_stores_only_digest(api, login):
    response = login()
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/api/v1" in cookie
    assert "Max-Age=28800" in cookie
    token = api.client.cookies.get(SESSION_COOKIE)
    assert token and len(token) >= 40
    assert set(response.json()) == {"id", "email", "role"}
    with api.sessions() as db:
        session = db.scalar(select(AuthSession))
        assert session.token_hash != token
        assert len(session.token_hash) == 64
        assert session.token_hash == token_hash(
            token, api.settings.session_secret.get_secret_value()
        )
        assert db.get(User, session.user_id).password_hash.startswith("$argon2id$")
    assert api.client.get(API + "/auth/me").json()["email"] == "editor@example.com"


@pytest.mark.parametrize(
    "email,password",
    [
        ("editor@example.com", "wrong"),
        ("missing@example.com", PASSWORD),
        ("disabled@example.com", PASSWORD),
    ],
)
def test_login_failure_does_not_disclose_user_existence(api, email, password):
    response = api.client.post(API + "/auth/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password"}
    assert SESSION_COOKIE not in response.cookies
    with api.sessions() as db:
        assert db.scalar(select(AuthSession)) is None
        assert db.scalar(select(AuditEvent)).event_type == "auth.login_failed"


def test_email_login_is_case_insensitive(api):
    response = api.client.post(
        API + "/auth/login", json={"email": "EDITOR@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["email"] == "editor@example.com"


def test_expired_session_rejected(api, login):
    login()
    with api.sessions() as db:
        session = db.scalar(select(AuthSession))
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert api.client.get(API + "/auth/me").status_code == 401


def test_tampered_session_cookie_cannot_authenticate(api, login):
    login()
    original = api.client.cookies.get(SESSION_COOKIE)
    tampered = ("x" if original[0] != "x" else "y") + original[1:]
    response = api.client.get(API + "/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={tampered}"})
    assert response.status_code == 401


def test_secure_cookie_flag_is_applied_when_configured(api, login):
    api.settings.cookie_secure = True
    response = login()
    assert "Secure" in response.headers["set-cookie"]


def test_disabled_user_loses_access_to_existing_session(api, login):
    login()
    assert api.client.get(API + "/auth/me").status_code == 200
    with api.sessions() as db:
        user = db.get(User, api.user_ids["editor"])
        user.active = False
        db.commit()
    assert api.client.get(API + "/auth/me").status_code == 401


def test_logout_revokes_server_session_and_deletes_cookie(api, login):
    login()
    raw_token = api.client.cookies.get(SESSION_COOKIE)
    response = api.client.post(API + "/auth/logout", headers={"Origin": ORIGIN})
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert api.client.get(API + "/auth/me").status_code == 401
    # A stolen old cookie must remain unusable even after it is manually replayed.
    replay = api.client.get(API + "/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={raw_token}"})
    assert replay.status_code == 401
    with api.sessions() as db:
        assert db.scalar(select(AuthSession)).revoked_at is not None


@pytest.mark.parametrize("origin", [None, "https://attacker.invalid", "null"])
def test_cookie_mutations_require_allowed_origin(api, login, origin):
    login()
    headers = {"Origin": origin} if origin is not None else {}
    response = api.client.post(API + "/projects", json={"name": "Untrusted"}, headers=headers)
    assert response.status_code == 403
    with api.sessions() as db:
        assert db.scalar(select(Project)) is None


def test_login_disallows_untrusted_origin(api):
    response = api.client.post(
        API + "/auth/login",
        json={"email": "editor@example.com", "password": PASSWORD},
        headers={"Origin": "https://attacker.invalid"},
    )
    assert response.status_code == 403


def test_unauthenticated_project_routes_and_oversized_cookie_rejected(api):
    assert api.client.get(API + "/projects").status_code == 401
    assert create_project(api).status_code == 401
    assert api.client.get(API + f"/projects/{uuid4()}").status_code == 401
    response = api.client.get(API + "/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={'x' * 129}"})
    assert response.status_code == 401


def test_editor_creates_project_settings_and_secret_free_audit(api, login):
    login()
    response = create_project(api, name="  产品介绍  ", language="zh-CN", aspect_ratio="9:16")
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["name"] == "产品介绍"
    assert data["owner_id"] == str(api.user_ids["editor"])
    assert Decimal(data["budget_usd"]) == Decimal("5")
    assert data["approved_cap_usd"] is None
    assert Decimal(data["committed_cost_usd"]) == 0
    assert data["status"] == "draft"
    with api.sessions() as db:
        settings = db.scalar(select(ProjectSettings))
        assert settings.language == "zh-CN" and settings.aspect_ratio == "9:16"
        events = db.scalars(select(AuditEvent)).all()
        assert {x.event_type for x in events} == {"auth.login", "project.created"}
        serialized = json.dumps([x.details for x in events])
        assert PASSWORD not in serialized
        assert api.client.cookies.get(SESSION_COOKIE) not in serialized
        assert all(x.actor_user_id == api.user_ids["editor"] for x in events)


def test_projects_scoped_by_owner_and_admin_can_review_all(api, login):
    login()
    first = create_project(api).json()
    api.client.cookies.clear()
    login("other")
    second_response = create_project(api, name="Second owner")
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    listed = api.client.get(API + "/projects").json()
    assert [x["id"] for x in listed] == [second["id"]]
    assert api.client.get(API + f"/projects/{first['id']}").status_code == 404
    assert api.client.get(API + f"/projects/{uuid4()}").status_code == 404
    api.client.cookies.clear()
    login("admin")
    assert len(api.client.get(API + "/projects").json()) == 2
    assert api.client.get(API + f"/projects/{first['id']}").status_code == 200
    assert len(api.client.get(API + "/projects?limit=1&offset=1").json()) == 1


def test_viewer_cannot_create_project(api, login):
    login("viewer")
    assert create_project(api).status_code == 403
    assert api.client.get(API + "/projects").json() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": " "},
        {"name": "x" * 161},
        {"prompt": "x" * 50001},
        {"target_duration_seconds": 4},
        {"target_duration_seconds": 601},
        {"budget_usd": "-0.01"},
        {"budget_usd": "10001"},
        {"budget_usd": "0.12345"},
        {"budget_usd": "NaN"},
        {"aspect_ratio": "4:3"},
        {"language": "xx"},
        {"owner_id": str(uuid4())},
        {"approved_cap_usd": "999"},
        {"status": "completed"},
    ],
)
def test_project_validation_rejects_untrusted_settings(api, login, overrides):
    login()
    response = create_project(api, **overrides)
    assert response.status_code == 422
    with api.sessions() as db:
        assert db.scalar(select(Project)) is None


def test_validation_errors_do_not_echo_password_or_raw_input(api):
    secret = "secret-marker-" + "z" * 257
    response = api.client.post(API + "/auth/login", json={"email": "bad", "password": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert '"input"' not in response.text


@pytest.mark.parametrize("content_length", ["131073", "-1", "bogus"])
def test_declared_oversized_or_invalid_body_is_rejected(api, content_length):
    response = api.client.post(
        API + "/auth/login", content=b"{}", headers={"Content-Length": content_length}
    )
    assert response.status_code == 413


def test_streamed_body_limit_cannot_be_bypassed_with_false_content_length(api):
    def chunks():
        yield b'{"password":"'
        yield b"x" * 70000
        yield b"x" * 70000
        yield b'"}'

    response = api.client.post(
        API + "/auth/login",
        content=chunks(),
        headers={"Content-Length": "1", "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_login_limits_are_separate_and_fail_closed(api):
    api.settings.login_attempts_per_minute = 1
    payload = {"email": "editor@example.com", "password": "wrong"}
    assert api.client.post(API + "/auth/login", json=payload).status_code == 401
    blocked = api.client.post(API + "/auth/login", json=payload)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    api.limiter.unavailable = True
    assert api.client.post(API + "/auth/login", json=payload).status_code == 503


def test_global_api_rate_limit_and_broker_outage(api):
    api.settings.api_requests_per_minute = 1
    assert api.client.get(API + "/auth/me").status_code == 401
    assert api.client.get(API + "/auth/me").status_code == 429
    api.limiter.unavailable = True
    assert api.client.get(API + "/projects").status_code == 503
    assert api.client.get("/health/live").json() == {"status": "ok"}


def test_readiness_requires_schema_revision_and_redis(api):
    assert api.client.get("/health/live").status_code == 200
    assert api.client.get("/health/ready").status_code == 200
    api.limiter.ping_ok = False
    assert api.client.get("/health/ready").status_code == 503
    api.limiter.ping_ok = True
    with api.app.state.engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='unknown'"))
    assert api.client.get("/health/ready").status_code == 503
    with api.app.state.engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
    assert api.client.get("/health/ready").status_code == 503


def test_responses_use_security_headers_and_disallow_cross_origin_reads(api):
    response = api.client.get("/health/live")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(response.headers["x-request-id"]) == 24
    response = api.client.options(
        API + "/projects",
        headers={"Origin": "https://attacker.invalid", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_backend_secrets_are_not_returned_to_api_clients(api, login):
    login_response = login()
    project = create_project(api)
    responses = [
        login_response,
        project,
        api.client.get(API + "/auth/me"),
        api.client.get(API + "/projects"),
        api.client.get("/health/ready"),
    ]
    for response in responses:
        assert response.status_code in {200, 201}
        assert api.settings.session_secret.get_secret_value() not in response.text
        assert PASSWORD_HASH not in response.text
        assert api.client.cookies.get(SESSION_COOKIE) not in response.text
        assert "password_hash" not in response.text
