"""Real database serialization and authorization fencing for staged credentials."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.main import create_app
from app.models import AuditEvent, AuthSession, ProviderSecret, User
from app.provider_keys import read_staged_key
from app.security import SESSION_COOKIE, new_session
from conftest import PASSWORD, PASSWORD_HASH, FakeLimiter
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event, func, select
from test_generation_postgres import postgres_generation as postgres_generation

PATH = "/api/v1/settings/provider-keys/openai"
KEYS = ["offline-only-concurrent-openai-key-1234567", "offline-only-concurrent-openai-key-7654321"]


@pytest.fixture
def pg_keys(postgres_generation):
    fixture = postgres_generation
    settings = fixture.settings.model_copy(
        update={
            "provider_key_encryption_key": SecretStr(Fernet.generate_key().decode()),
            "paid_generation_enabled": False,
            "paid_approval_reference": "",
            "fal_key": SecretStr(""),
            "allowed_origins": ["http://localhost:3000"],
            "cookie_secure": False,
            "api_requests_per_minute": 1000,
        }
    )
    application = create_app(settings, engine=fixture.engine, limiter=FakeLimiter())
    tokens, users, sessions = [], [], []
    with fixture.sessions() as db:
        for index in range(2):
            user = User(
                email=f"key-admin-{index}@example.com", password_hash=PASSWORD_HASH, role="admin"
            )
            db.add(user)
            db.flush()
            token, digest, expiry = new_session(settings.session_secret.get_secret_value(), 1)
            auth = AuthSession(user_id=user.id, token_hash=digest, expires_at=expiry)
            db.add(auth)
            db.flush()
            users.append(user.id)
            sessions.append(auth.id)
            tokens.append(token)
        db.commit()

    def headers(actor=0):
        return {"Origin": "http://localhost:3000", "Cookie": f"{SESSION_COOKIE}={tokens[actor]}"}

    with TestClient(application, base_url="http://testserver") as client:
        yield SimpleNamespace(
            engine=fixture.engine,
            sessions=fixture.sessions,
            settings=settings,
            client=client,
            headers=headers,
            user_ids=users,
            auth_ids=sessions,
        )


@pytest.mark.integration
def test_two_admins_cannot_overwrite_same_provider_revision(pg_keys, monkeypatch):
    import app.provider_keys_api as routes

    # Two slots model two independent API processes; production retains one slot
    # per process while PostgreSQL must also serialize across processes.
    monkeypatch.setattr(routes, "PASSWORD_SLOTS", threading.BoundedSemaphore(2))
    barrier = threading.Barrier(2)

    def save(actor):
        barrier.wait(timeout=10)
        return pg_keys.client.put(
            PATH,
            headers=pg_keys.headers(actor),
            json={"api_key": KEYS[actor], "current_password": PASSWORD, "expected_revision": 0},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, range(2)))
    assert sorted(row.status_code for row in results) == [200, 409]
    winner = next(index for index, row in enumerate(results) if row.status_code == 200)
    with pg_keys.sessions() as db:
        row = db.get(ProviderSecret, "openai")
        assert row.revision == 1 and row.updated_by == pg_keys.user_ids[winner]
        assert read_staged_key(db, pg_keys.settings, "openai").get_secret_value() == KEYS[winner]
        assert db.scalar(select(func.count()).select_from(ProviderSecret)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "provider_key.saved")
            )
            == 1
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "revocation", ["inactive", "demoted", "session_revoked", "session_expired"]
)
def test_waiting_key_change_rechecks_actor_and_session_after_locks(pg_keys, revocation):
    fixture = pg_keys
    reached = threading.Event()

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "FOR UPDATE" in statement and "FROM users" in statement:
            reached.set()

    with fixture.sessions() as holder:
        actor = holder.scalar(select(User).where(User.id == fixture.user_ids[0]).with_for_update())
        event.listen(fixture.engine, "before_cursor_execute", observe)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(
                    fixture.client.put,
                    PATH,
                    headers=fixture.headers(),
                    json={"api_key": KEYS[0], "current_password": PASSWORD, "expected_revision": 0},
                )
                locked = reached.wait(timeout=10)
                if locked:
                    if revocation == "inactive":
                        actor.active = False
                    elif revocation == "demoted":
                        actor.role = "editor"
                    else:
                        auth = holder.get(AuthSession, fixture.auth_ids[0])
                        if revocation == "session_revoked":
                            auth.revoked_at = datetime.now(UTC)
                        else:
                            auth.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    holder.commit()
                else:
                    holder.rollback()
                assert locked, "Mutation did not reach the actor lock after authentication"
                result = pending.result(timeout=10)
        finally:
            event.remove(fixture.engine, "before_cursor_execute", observe)
            holder.rollback()
    assert result.status_code == (403 if revocation in {"inactive", "demoted"} else 401)
    with fixture.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProviderSecret)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "provider_key.saved")
            )
            == 0
        )
