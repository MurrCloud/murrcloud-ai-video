"""Administrator-only key staging with reauthentication and no external calls."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import administrator, get_db
from app.limits import LimiterUnavailable, RateLimitExceeded
from app.models import AuditEvent, AuthSession, User
from app.provider_keys import ProviderKeyError, save, status
from app.security import PASSWORD_SLOTS, aware_utc, verify_password

router = APIRouter(prefix="/api/v1/settings/provider-keys", tags=["provider credentials"])
Provider = Literal["openai", "fal"]


class MutationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    current_password: SecretStr = Field(min_length=1, max_length=256, repr=False)
    expected_revision: int = Field(ge=0, le=2**31 - 2, strict=True)


class KeyIn(MutationIn):
    api_key: SecretStr = Field(min_length=16, max_length=512, repr=False)

    @field_validator("api_key", mode="before")
    @classmethod
    def trim_key(cls, value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, SecretStr):
            return SecretStr(value.get_secret_value().strip())
        return value

    @field_validator("api_key")
    @classmethod
    def safe_key(cls, value):
        raw = value.get_secret_value()
        if not all(33 <= ord(character) <= 126 for character in raw):
            raise ValueError("API keys must contain only printable characters without spaces")
        return value


def authenticate_actor(db, request, user, provider, payload):
    actor = db.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not actor or not actor.active or actor.role != "admin":
        raise HTTPException(403, "Administrator role required")
    auth = db.scalar(
        select(AuthSession)
        .where(AuthSession.id == request.state.auth_session.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not auth
        or auth.user_id != actor.id
        or auth.revoked_at is not None
        or aware_utc(auth.expires_at) <= datetime.now(UTC)
    ):
        raise HTTPException(401, "Session expired or invalid")
    if not verify_password(payload.current_password.get_secret_value(), actor.password_hash):
        db.add(
            AuditEvent(
                event_type="provider_key.reauthentication_failed",
                actor_user_id=actor.id,
                entity_type="provider_credential",
                details={"provider": provider},
            )
        )
        db.commit()
        raise HTTPException(403, "Current password is incorrect")
    return actor


def mutate(request, db, user, provider, payload, key):
    try:
        request.app.state.limiter.check(f"provider-key-user:{user.id}", 5)
    except RateLimitExceeded:
        raise HTTPException(429, "Too many key changes", headers={"Retry-After": "60"}) from None
    except LimiterUnavailable:
        raise HTTPException(503, "Service temporarily unavailable") from None
    if not PASSWORD_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Please retry shortly", headers={"Retry-After": "5"})
    try:
        actor = authenticate_actor(db, request, user, provider, payload)
        try:
            save(db, request.app.state.settings, actor, provider, payload.expected_revision, key)
        except ProviderKeyError as exc:
            raise HTTPException(exc.status, exc.code) from None
        result = status(db, request.app.state.settings)
        db.commit()
        return result
    finally:
        PASSWORD_SLOTS.release()


@router.get("")
def key_status(request: Request, _user=Depends(administrator), db: Session = Depends(get_db)):
    return status(db, request.app.state.settings)


@router.put("/{provider}")
def save_key(
    provider: Provider,
    payload: KeyIn,
    request: Request,
    user=Depends(administrator),
    db: Session = Depends(get_db),
):
    return mutate(request, db, user, provider, payload, payload.api_key)


@router.delete("/{provider}")
def remove_key(
    provider: Provider,
    payload: MutationIn,
    request: Request,
    user=Depends(administrator),
    db: Session = Depends(get_db),
):
    return mutate(request, db, user, provider, payload, None)
