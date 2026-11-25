"""Fake-key HTTP/proxy roundtrips restricted to disposable Compose services."""

import json
import os
import secrets
from datetime import UTC, datetime

import httpx
import pytest
from app.db import make_engine, make_session_factory
from app.models import (
    ActualUsageCost,
    AuditEvent,
    AuthSession,
    DirectorAttempt,
    DirectorJob,
    GenerationAttempt,
    GenerationJob,
    ProviderSecret,
    RenderingJob,
    User,
)
from app.security import hash_password
from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, update
from test_live_workspace import isolated_surface, ok

PATH = "/api/v1/settings/provider-keys"
WORK = (
    GenerationJob,
    GenerationAttempt,
    DirectorJob,
    DirectorAttempt,
    RenderingJob,
    ActualUsageCost,
)


def work_counts(sessions):
    with sessions() as db:
        return {
            table.__tablename__: db.scalar(select(func.count()).select_from(table))
            for table in WORK
        }


@pytest.mark.integration
@pytest.mark.parametrize("surface", ["api", "frontend"])
def test_live_provider_keys_are_encrypted_staged_and_never_activate_work(surface):
    base, url = isolated_surface(surface)
    master = os.environ.get("VIDEO_TEST_PROVIDER_KEY_ENCRYPTION_KEY")
    assert master, "An ephemeral test-only provider encryption key must be configured"
    cipher = Fernet(master.encode("ascii"))
    engine = make_engine(url)
    sessions = make_session_factory(engine)
    marker = "key-settings-" + secrets.token_hex(8)
    password = secrets.token_urlsafe(24)
    emails = {role: f"{marker}-{role}@example.com" for role in ("admin", "editor")}
    users = {}
    fake_keys = {
        provider: "offline-only-" + marker + "-" + provider for provider in ("openai", "fal")
    }
    with sessions() as db:
        for role, email in emails.items():
            user = User(email=email, role=role, password_hash=hash_password(password))
            db.add(user)
            db.flush()
            users[role] = user.id
        db.commit()
    before = work_counts(sessions)
    origin = "http://localhost:8100" if surface == "api" else "http://localhost:3100"
    headers = {"Origin": origin}
    try:
        with (
            httpx.Client(base_url=base, timeout=20, trust_env=False) as admin,
            httpx.Client(base_url=base, timeout=20, trust_env=False) as editor,
        ):
            assert admin.get(PATH).status_code == 401
            ok(
                editor.post(
                    "/api/v1/auth/login", json={"email": emails["editor"], "password": password}
                )
            )
            assert editor.get(PATH).status_code == 403
            assert (
                editor.put(
                    PATH + "/openai",
                    headers=headers,
                    json={
                        "api_key": fake_keys["openai"],
                        "current_password": password,
                        "expected_revision": 0,
                    },
                ).status_code
                == 403
            )
            ok(
                admin.post(
                    "/api/v1/auth/login", json={"email": emails["admin"], "password": password}
                )
            )
            settings = ok(admin.get("/api/v1/settings"))
            assert not any(settings["features"].values())
            original_providers = settings["providers"]
            metadata = ok(admin.get(PATH))
            assert metadata["storage_available"] is True
            assert metadata["paid_generation_enabled"] is metadata["director_enabled"] is False
            assert all(not row["stored"] for row in metadata["providers"]), (
                "Never overwrite another fixture's staged keys"
            )
            for provider in ("openai", "fal"):
                prior = next(row for row in metadata["providers"] if row["provider"] == provider)
                response = admin.put(
                    PATH + "/" + provider,
                    headers=headers,
                    json={
                        "api_key": fake_keys[provider],
                        "current_password": password,
                        "expected_revision": prior["revision"],
                    },
                )
                metadata = ok(response)
                assert fake_keys[provider] not in response.text and password not in response.text
                row = next(row for row in metadata["providers"] if row["provider"] == provider)
                assert row["stored"] and row["revision"] == prior["revision"] + 1
                assert row["usage_enabled"] is False
                with sessions() as db:
                    stored = db.get(ProviderSecret, provider)
                    assert stored.updated_by == users["admin"]
                    assert fake_keys[provider] not in stored.encrypted_value
                    decoded = json.loads(cipher.decrypt(stored.encrypted_value.encode("ascii")))
                    assert decoded == {
                        "format": "studio-provider-key-v1",
                        "provider": provider,
                        "key": fake_keys[provider],
                    }
                    event = db.scalar(
                        select(AuditEvent)
                        .where(
                            AuditEvent.actor_user_id == users["admin"],
                            AuditEvent.event_type == "provider_key.saved",
                        )
                        .order_by(AuditEvent.created_at.desc())
                    )
                    assert fake_keys[provider] not in json.dumps(event.details)
            reloaded = ok(admin.get(PATH))
            assert all(row["stored"] for row in reloaded["providers"])
            for key in fake_keys.values():
                assert key not in json.dumps(reloaded)
            for provider in ("openai", "fal"):
                prior = next(row for row in metadata["providers"] if row["provider"] == provider)
                metadata = ok(
                    admin.request(
                        "DELETE",
                        PATH + "/" + provider,
                        headers=headers,
                        json={"current_password": password, "expected_revision": prior["revision"]},
                    )
                )
                row = next(row for row in metadata["providers"] if row["provider"] == provider)
                assert not row["stored"] and row["revision"] == prior["revision"] + 1
            final = ok(admin.get("/api/v1/settings"))
            assert final["features"] == settings["features"]
            assert final["providers"] == original_providers
            assert not any(final["features"].values())
            assert ok(admin.get("/api/v1/generation-config"))["paid_generation_enabled"] is False
            assert ok(admin.get("/api/v1/director-config"))["director_enabled"] is False
        assert work_counts(sessions) == before
    finally:
        with sessions() as db:
            # Only these random fixture actors' values may be removed after a
            # failed assertion. Retain provider tombstones and monotonic revisions.
            db.execute(
                update(ProviderSecret)
                .where(ProviderSecret.updated_by.in_(users.values()))
                .values(
                    encrypted_value=None,
                    updated_by=None,
                    revision=ProviderSecret.revision + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id.in_(users.values())))
            db.execute(delete(AuthSession).where(AuthSession.user_id.in_(users.values())))
            db.execute(delete(User).where(User.id.in_(users.values())))
            db.commit()
        engine.dispose()
