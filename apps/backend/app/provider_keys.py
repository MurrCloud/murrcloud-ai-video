"""Encrypted, staged provider credentials. No provider transport or dispatch lives here."""

import json
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy import select

from app.models import AuditEvent, ProviderSecret
from app.security import aware_utc

PROVIDERS = ("openai", "fal")


class ProviderKeyError(Exception):
    """Fixed public messages only; never include a key, ciphertext or crypto error."""

    def __init__(self, code, status=503):
        super().__init__(code)
        self.code = code
        self.status = status


def cipher(settings):
    try:
        value = settings.provider_key_encryption_key.get_secret_value()
        if not value:
            raise ValueError("missing")
        return Fernet(value.encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        raise ProviderKeyError("Secure key storage is unavailable") from None


def storage_available(settings):
    try:
        cipher(settings)
        return True
    except ProviderKeyError:
        return False


def status(db, settings):
    rows = {row.provider: row for row in db.scalars(select(ProviderSecret)).all()}
    return {
        "storage_available": storage_available(settings),
        "paid_generation_enabled": settings.paid_generation_enabled,
        "director_enabled": settings.director_enabled,
        "providers": [
            {
                "provider": provider,
                "stored": bool(rows.get(provider) and rows[provider].encrypted_value),
                "revision": rows[provider].revision if provider in rows else 0,
                "updated_at": aware_utc(rows[provider].updated_at)
                if provider in rows and rows[provider].updated_at
                else None,
                "runtime_configured": bool(
                    (settings.openai_key if provider == "openai" else settings.fal_key)
                    .get_secret_value()
                    .strip()
                ),
                "usage_enabled": settings.director_enabled
                if provider == "openai"
                else settings.paid_generation_enabled,
            }
            for provider in PROVIDERS
        ],
    }


def locked_row(db, provider):
    # Explicit migrations seed both rows. This also permits isolated metadata-only
    # test databases and serializes concurrent first saves without duplicate rows.
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise ProviderKeyError("Secure key storage is unavailable")
    db.execute(insert(ProviderSecret).values(provider=provider).on_conflict_do_nothing())
    return db.scalar(
        select(ProviderSecret)
        .where(ProviderSecret.provider == provider)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def save(db, settings, actor, provider, expected_revision, key: SecretStr | None):
    vault = cipher(settings)
    row = locked_row(db, provider)
    if row is None or row.revision != expected_revision:
        raise ProviderKeyError("Saved keys changed. Reload settings and try again.", 409)
    if key is None:
        row.encrypted_value = None
    else:
        # Authenticated provider binding stops ciphertext being moved to another row.
        raw = json.dumps(
            {
                "format": "studio-provider-key-v1",
                "provider": provider,
                "key": key.get_secret_value(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        row.encrypted_value = vault.encrypt(raw).decode("ascii")
    row.revision += 1
    row.updated_at = datetime.now(UTC)
    row.updated_by = actor.id
    db.add(
        AuditEvent(
            event_type="provider_key.removed" if key is None else "provider_key.saved",
            actor_user_id=actor.id,
            entity_type="provider_credential",
            details={"provider": provider, "revision": row.revision},
        )
    )
    db.flush()


def read_staged_key(db, settings, provider):
    """Internal-only future promotion primitive; no router returns this value.

    Reading does not authorize provider use. Live factories deliberately continue
    to consume separately approved environment credentials.
    """
    if provider not in PROVIDERS:
        raise ProviderKeyError("Unsupported provider", 404)
    row = db.get(ProviderSecret, provider)
    if row is None or not row.encrypted_value:
        return None
    try:
        if len(row.encrypted_value) > 2048:
            raise ValueError("oversized payload")
        value = json.loads(cipher(settings).decrypt(row.encrypted_value.encode("ascii")))
        if (
            value.get("format") != "studio-provider-key-v1"
            or value.get("provider") != provider
            or not isinstance(value.get("key"), str)
        ):
            raise ValueError("invalid payload")
        return SecretStr(value["key"])
    except (InvalidToken, ValueError, TypeError, UnicodeError, AttributeError):
        raise ProviderKeyError("Stored key cannot be decrypted") from None
