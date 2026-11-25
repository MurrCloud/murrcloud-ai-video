from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.dependencies import current_user, get_db
from app.models import MediaAsset, Project, User
from app.production_storage import artifact_path
from app.reference_storage import reference_path
from app.storage import StorageError, local_asset_path

router = APIRouter(prefix="/api/v1", tags=["assets"])


def asset_response(asset_id, request, user, db, *, preview=False):
    asset = db.get(MediaAsset, asset_id)
    project = db.get(Project, asset.project_id) if asset else None
    if not asset or not project or (user.role != "admin" and project.owner_id != user.id):
        raise HTTPException(404, "Asset not found")
    kinds = (
        {"clip", "final_video", "reference_image", "logo"}
        if preview
        else {"clip", "final_video", "narration", "subtitles", "music"}
    )
    if asset.status != "ready" or asset.storage_backend != "local" or asset.kind not in kinds:
        raise HTTPException(404, "Asset not available")
    if not asset.object_key.startswith(str(asset.project_id) + "/"):
        raise HTTPException(404, "Asset not available")
    try:
        if asset.kind == "clip":
            path = local_asset_path(request.app.state.settings, asset.object_key)
        elif asset.kind in {"reference_image", "logo"}:
            path = reference_path(request.app.state.settings, asset.object_key)
        else:
            path = artifact_path(request.app.state.settings, asset.object_key)
    except StorageError:
        raise HTTPException(404, "Asset not available") from None
    return FileResponse(
        path,
        media_type=asset.mime_type,
        content_disposition_type="inline" if preview else "attachment",
        filename=f"{asset.kind}-{asset.id}{path.suffix}",
    )


@router.get("/assets/{asset_id}/download")
def download_asset(
    asset_id: UUID,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return asset_response(asset_id, request, user, db)


@router.get("/assets/{asset_id}/preview")
def preview_asset(
    asset_id: UUID,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return asset_response(asset_id, request, user, db, preview=True)
