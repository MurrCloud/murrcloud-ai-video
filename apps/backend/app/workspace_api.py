"""Account settings and administrator-only operational evidence, without secrets."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.dependencies import administrator, audit, current_user, get_db
from app.ingestion import MAX_SCRIPT_CHARS, MAX_UPLOAD_BYTES
from app.limits import LimiterUnavailable, RateLimitExceeded
from app.models import AuditEvent, AuthSession, User
from app.monitoring import operations
from app.security import PASSWORD_SLOTS, SESSION_COOKIE, hash_password, verify_password

router = APIRouter(prefix="/api/v1", tags=["workspace"])


@router.get("/settings")
def settings(request: Request, user=Depends(current_user)):
    config = request.app.state.settings
    return {
        "user": {"id": user.id, "email": user.email, "role": user.role},
        "security": {"session_hours": config.session_hours, "cookie_secure": config.cookie_secure},
        "features": {
            key: getattr(config, key)
            for key in (
                "paid_generation_enabled",
                "director_enabled",
                "narration_enabled",
                "render_enabled",
                "s3_enabled",
            )
        },
        "limits": {
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_script_characters": MAX_SCRIPT_CHARS,
            "max_media_bytes": config.max_media_bytes,
            "storage_max_bytes": config.storage_max_bytes,
            "render_scratch_max_bytes": config.render_scratch_max_bytes,
            "render_concurrency": 1,
            "ffmpeg_threads": 1,
            "render_timeout_seconds": config.render_timeout_seconds,
        },
        "providers": {
            "fal_configured": bool(config.fal_key.get_secret_value()),
            "openai_configured": bool(config.openai_key.get_secret_value()),
            "tts_provider": "edge",
        },
    }


class PasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    current_password: str = Field(min_length=1, max_length=256, repr=False)
    new_password: str = Field(min_length=12, max_length=256, repr=False)
    confirm_password: str = Field(min_length=12, max_length=256, repr=False)


@router.post("/account/password", status_code=204)
def change_password(
    payload: PasswordIn, request: Request, user=Depends(current_user), db: Session = Depends(get_db)
):
    if payload.new_password != payload.confirm_password:
        raise HTTPException(422, "Passwords do not match")
    try:
        request.app.state.limiter.check(f"password-user:{user.id}", 5)
    except RateLimitExceeded:
        raise HTTPException(
            429, "Too many password attempts", headers={"Retry-After": "60"}
        ) from None
    except LimiterUnavailable:
        raise HTTPException(503, "Service temporarily unavailable") from None
    if not PASSWORD_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Please retry shortly", headers={"Retry-After": "5"})
    try:
        locked = db.scalar(
            select(User)
            .where(User.id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not locked
            or not locked.active
            or not verify_password(payload.current_password, locked.password_hash)
        ):
            audit(db, "auth.password_change_failed", user)
            db.commit()
            raise HTTPException(403, "Current password is incorrect")
        if payload.current_password == payload.new_password:
            raise HTTPException(422, "Choose a different password")
        locked.password_hash = hash_password(payload.new_password)
        db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        audit(db, "auth.password_changed", user)
        db.commit()
    finally:
        PASSWORD_SLOTS.release()
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/api/v1",
        secure=request.app.state.settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/operations")
def operational_summary(
    request: Request, _user=Depends(administrator), db: Session = Depends(get_db)
):
    return operations(db, request.app.state.settings)


@router.get("/operations/audit")
def audit_history(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    _user=Depends(administrator),
    db: Session = Depends(get_db),
):
    # Details can contain private planning context and operator evidence. Return
    # only event metadata; never serialize a whole ORM audit object.
    rows = db.scalars(
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [
            {
                key: getattr(row, key)
                for key in (
                    "id",
                    "event_type",
                    "project_id",
                    "entity_type",
                    "entity_id",
                    "created_at",
                )
            }
            for row in rows
        ],
        "total": db.scalar(select(func.count()).select_from(AuditEvent)),
        "limit": limit,
        "offset": offset,
    }
