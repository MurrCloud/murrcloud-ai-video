"""Staged credentials stay encrypted, administrator-only and disconnected from paid work."""

import json

import pytest
from app.models import AuditEvent, DirectorJob, GenerationJob, ProviderSecret, RenderingJob
from app.provider_keys import ProviderKeyError, read_staged_key
from conftest import ORIGIN, PASSWORD
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select

PATH = "/api/v1/settings/provider-keys"
HEADERS = {"Origin": ORIGIN}
KEY = "synthetic-provider-setting-" * 2
SECOND_KEY = "offline-only-replacement-provider-key-987654321"


@pytest.fixture
def vault(api):
    settings = api.settings.model_copy(
        update={"provider_key_encryption_key": SecretStr(Fernet.generate_key().decode())}
    )
    api.app.state.settings = settings
    return settings


def payload(revision=0, key=KEY, password=PASSWORD):
    return {"api_key": key, "current_password": password, "expected_revision": revision}


def remove(api, provider="openai", revision=1):
    return api.client.request(
        "DELETE",
        PATH + "/" + provider,
        json={"current_password": PASSWORD, "expected_revision": revision},
        headers=HEADERS,
    )


def test_key_management_requires_admin_for_reads_and_mutations(api, login, vault):
    assert api.client.get(PATH).status_code == 401
    assert api.client.put(PATH + "/openai", json=payload(), headers=HEADERS).status_code == 401
    for role in ("editor", "viewer", "other"):
        login(role)
        assert api.client.get(PATH).status_code == 403
        assert api.client.put(PATH + "/openai", json=payload(), headers=HEADERS).status_code == 403
        assert remove(api).status_code == 403
    with api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProviderSecret)) == 0


def test_saved_key_is_encrypted_and_never_returned_or_activated(api, login, vault, caplog):
    def forbidden(*args, **kwargs):
        raise AssertionError("Saving a key must not construct or contact a provider")

    for factory in (
        "provider_factory",
        "director_factory",
        "generation_dispatcher",
        "director_dispatcher",
        "production_dispatcher",
    ):
        setattr(api.app.state, factory, forbidden)
    login("admin")
    before = api.client.get(PATH).json()
    assert before["storage_available"] is True
    assert all(not row["stored"] and row["revision"] == 0 for row in before["providers"])
    response = api.client.put(PATH + "/openai", json=payload(), headers=HEADERS)
    assert response.status_code == 200
    metadata = response.json()
    stored = next(row for row in metadata["providers"] if row["provider"] == "openai")
    assert stored["stored"] and stored["revision"] == 1 and stored["updated_at"]
    assert stored["runtime_configured"] is stored["usage_enabled"] is False
    assert metadata["paid_generation_enabled"] is metadata["director_enabled"] is False
    for output in (response.text, api.client.get(PATH).text, caplog.text):
        assert KEY not in output and PASSWORD not in output
        assert vault.provider_key_encryption_key.get_secret_value() not in output
    with api.sessions() as db:
        row = db.get(ProviderSecret, "openai")
        assert KEY not in row.encrypted_value
        assert row.updated_by == api.user_ids["admin"]
        assert read_staged_key(db, vault, "openai").get_secret_value() == KEY
        assert KEY not in repr(read_staged_key(db, vault, "openai"))
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "provider_key.saved")
        ).all()
        assert len(events) == 1
        assert events[0].details == {"provider": "openai", "revision": 1}
        assert row.encrypted_value not in json.dumps(events[0].details)
        for model in (GenerationJob, DirectorJob, RenderingJob):
            assert db.scalar(select(func.count()).select_from(model)) == 0
    assert not any(api.client.get("/api/v1/settings").json()["features"].values())
    assert api.client.get("/api/v1/generation-config").json()["provider_configured"] is False
    assert api.client.get("/api/v1/director-config").json()["provider_configured"] is False


@pytest.mark.parametrize("provider", ["openai", "fal"])
def test_replace_remove_and_stale_forms_preserve_monotonic_history(api, login, vault, provider):
    login("admin")
    assert api.client.put(PATH + "/" + provider, json=payload(), headers=HEADERS).status_code == 200
    assert (
        api.client.put(
            PATH + "/" + provider, json=payload(0, SECOND_KEY), headers=HEADERS
        ).status_code
        == 409
    )
    assert (
        api.client.put(
            PATH + "/" + provider, json=payload(1, SECOND_KEY), headers=HEADERS
        ).status_code
        == 200
    )
    assert remove(api, provider, 1).status_code == 409
    assert remove(api, provider, 2).status_code == 200
    # Reloading a stale saved form cannot resurrect the deleted key.
    # Six writes would exhaust the limit, so a read proves the final state here.
    result = next(
        row for row in api.client.get(PATH).json()["providers"] if row["provider"] == provider
    )
    assert result["stored"] is False and result["revision"] == 3
    with api.sessions() as db:
        row = db.get(ProviderSecret, provider)
        assert row.encrypted_value is None
        assert read_staged_key(db, vault, provider) is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type.like("provider_key.%"))
            )
            == 3
        )


def test_removal_tombstone_rejects_old_revision_and_new_save_can_use_current_revision(
    api, login, vault
):
    login("admin")
    assert remove(api, revision=0).status_code == 200
    assert api.client.put(PATH + "/openai", json=payload(), headers=HEADERS).status_code == 409
    assert api.client.put(PATH + "/openai", json=payload(1), headers=HEADERS).status_code == 200


def test_wrong_password_missing_storage_and_redis_failure_never_write(api, login, vault):
    login("admin")
    assert (
        api.client.put(
            PATH + "/openai", json=payload(password="wrong-password"), headers=HEADERS
        ).status_code
        == 403
    )
    with api.sessions() as db:
        failed = db.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "provider_key.reauthentication_failed"
            )
        )
        assert failed and failed.details == {"provider": "openai"}
    api.app.state.settings = vault.model_copy(update={"provider_key_encryption_key": SecretStr("")})
    assert api.client.get(PATH).json()["storage_available"] is False
    assert api.client.put(PATH + "/openai", json=payload(), headers=HEADERS).status_code == 503
    api.app.state.settings = vault
    api.limiter.unavailable = True
    assert api.client.put(PATH + "/openai", json=payload(), headers=HEADERS).status_code == 503
    with api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProviderSecret)) == 0


def test_origin_and_reauthentication_rate_limits_remain_enforced(api, login, vault):
    login("admin")
    assert api.client.put(PATH + "/openai", json=payload()).status_code == 403
    assert (
        api.client.put(
            PATH + "/openai", json=payload(), headers={"Origin": "https://unapproved.invalid"}
        ).status_code
        == 403
    )
    for _ in range(5):
        assert (
            api.client.put(
                PATH + "/openai", json=payload(password="wrong-password"), headers=HEADERS
            ).status_code
            == 403
        )
    assert api.client.put(PATH + "/openai", json=payload(), headers=HEADERS).status_code == 429


@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "short"},
        {"api_key": KEY + " " + KEY},
        {"api_key": "x" * 513},
        {"api_key": KEY + "\x00"},
        {"api_key": KEY + "\n" + KEY},
        {"api_key": KEY + "中"},
        {"api_key": [KEY]},
        {"current_password": "x" * 257},
        {"expected_revision": True},
        {"expected_revision": "0"},
        {"expected_revision": -1},
        {"paid_generation_enabled": True},
        {"director_enabled": True},
        {KEY: PASSWORD},
    ],
)
def test_invalid_input_never_echoes_secret_values_or_field_names(api, login, vault, change):
    login("admin")
    response = api.client.put(PATH + "/openai", json={**payload(), **change}, headers=HEADERS)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid provider key request"}
    assert KEY not in response.text and PASSWORD not in response.text
    with api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProviderSecret)) == 0


def test_api_key_paste_trims_edges_but_never_password(api, login, vault):
    login("admin")
    assert (
        api.client.put(
            PATH + "/fal", json=payload(key="\n " + KEY + " \r\n"), headers=HEADERS
        ).status_code
        == 200
    )
    with api.sessions() as db:
        assert read_staged_key(db, vault, "fal").get_secret_value() == KEY
    response = api.client.put(
        PATH + "/openai", json=payload(password=" " + PASSWORD + " "), headers=HEADERS
    )
    assert response.status_code == 403


def test_unsupported_provider_is_generic_and_does_not_create_rows(api, login, vault):
    login("admin")
    response = api.client.put(PATH + "/" + KEY, json=payload(), headers=HEADERS)
    assert response.status_code == 422 and KEY not in response.text
    with api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProviderSecret)) == 0


def test_ciphertext_is_bound_to_provider_and_authenticated(api, login, vault):
    login("admin")
    assert api.client.put(PATH + "/openai", json=payload(), headers=HEADERS).status_code == 200
    with api.sessions() as db:
        row = db.get(ProviderSecret, "openai")
        ciphertext = row.encrypted_value
        db.add(ProviderSecret(provider="fal", encrypted_value=ciphertext, revision=1))
        db.commit()
        with pytest.raises(ProviderKeyError, match="Stored key cannot be decrypted"):
            read_staged_key(db, vault, "fal")
        row.encrypted_value = ciphertext[:-3] + "xxx"
        db.commit()
        with pytest.raises(ProviderKeyError, match="Stored key cannot be decrypted"):
            read_staged_key(db, vault, "openai")
        row.encrypted_value = ciphertext
        db.commit()
        wrong_master = vault.model_copy(
            update={"provider_key_encryption_key": SecretStr(Fernet.generate_key().decode())}
        )
        with pytest.raises(ProviderKeyError, match="Stored key cannot be decrypted"):
            read_staged_key(db, wrong_master, "openai")
