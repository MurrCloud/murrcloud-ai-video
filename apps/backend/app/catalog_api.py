"""Authenticated catalog discovery and limited admin switches."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.catalog import serialize_model
from app.dependencies import current_user, get_db
from app.models import AuditEvent, ProviderConfiguration, User, VideoModel

router = APIRouter(prefix="/api/v1", tags=["models"])


class ModelSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    is_default: bool | None = None


@router.get("/models")
def list_models(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(VideoModel, ProviderConfiguration.enabled)
        .join(ProviderConfiguration, VideoModel.provider == ProviderConfiguration.provider)
        .order_by(VideoModel.is_default.desc(), VideoModel.display_name)
    ).all()
    result = []
    for row, provider_enabled in rows:
        data = serialize_model(row)
        data["enabled"] = bool(row.enabled and provider_enabled)
        result.append(data)
    return result


@router.get("/generation-config")
def generation_config(request: Request, user: User = Depends(current_user)):
    settings = request.app.state.settings
    configured = bool(settings.fal_key.get_secret_value().strip())
    return {
        "paid_generation_enabled": settings.paid_generation_enabled,
        "provider_configured": configured,
        "requires_operator_approval": not settings.paid_generation_enabled,
        "reference_uploads_available": False,
    }


@router.patch("/models/{model_id:path}")
def switch_model(
    model_id: str,
    payload: ModelSwitch,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if user.role != "admin":
        raise HTTPException(403, "Administrator role required")
    # Lock the provider row to serialize changes to the catalog's unique default.
    model = db.scalar(select(VideoModel).where(VideoModel.model_id == model_id))
    if not model:
        raise HTTPException(404, "Model not found")
    db.scalar(
        select(ProviderConfiguration)
        .where(ProviderConfiguration.provider == model.provider)
        .with_for_update()
    )
    db.refresh(model)
    if payload.enabled is True and "text_to_video" not in model.supported_generation_modes:
        raise HTTPException(409, "Reference image ingestion is not enabled in Phase 2")
    if payload.enabled is not None:
        model.enabled = payload.enabled
        if not model.enabled:
            model.is_default = False
    if payload.is_default is True:
        if not model.enabled:
            raise HTTPException(409, "A disabled model cannot be the default")
        db.execute(
            update(VideoModel).where(VideoModel.is_default.is_(True)).values(is_default=False)
        )
        db.flush()
        model.is_default = True
    elif payload.is_default is False:
        model.is_default = False
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="model.updated",
            entity_type="model",
            entity_id=model.id,
            details=payload.model_dump(exclude_none=True),
        )
    )
    db.commit()
    return serialize_model(model)


@router.get("/director-config")
def director_config(request: Request, user: User = Depends(current_user)):
    settings = request.app.state.settings
    configured = bool(
        settings.openai_key.get_secret_value().strip()
        and settings.director_model.strip()
        and settings.director_approval_reference.strip()
    )
    return {
        "director_enabled": settings.director_enabled,
        "provider_configured": configured,
        "requires_operator_approval": not settings.director_enabled,
        "model": settings.director_model or None,
    }
