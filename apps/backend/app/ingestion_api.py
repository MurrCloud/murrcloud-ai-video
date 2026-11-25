import hashlib
import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.dependencies import current_user, editor, get_db
from app.generation import GenerationError, owned_project, require_open_project
from app.ingestion import (
    MAX_UPLOAD_BYTES,
    IngestionError,
    canonicalize_reference,
    parse_docx,
    safe_source_name,
)
from app.models import AuditEvent, MediaAsset, Project, User
from app.reference_storage import remove_uncommitted_reference, store_reference
from app.storage import StorageError

router = APIRouter(prefix="/api/v1", tags=["imports"])
logger = logging.getLogger("video.ingestion")


def reference_projection(asset):
    metadata = asset.metadata_json or {}
    return {
        "id": str(asset.id),
        "project_id": str(asset.project_id),
        "original_filename": asset.original_filename,
        "mime_type": asset.mime_type,
        "size_bytes": asset.size_bytes,
        "metadata_json": {
            "width": metadata.get("width"),
            "height": metadata.get("height"),
            "caption": metadata.get("caption", ""),
        },
        "preview_url": f"/api/v1/assets/{asset.id}/preview",
    }


def project_or_404(db, project_id, user, *, lock=False, active=False):
    try:
        project = owned_project(db, project_id, user, lock=lock)
        return require_open_project(project) if active else project
    except GenerationError as exc:
        raise HTTPException(exc.status, exc.code) from None


def _cleanup_failed_reference(request, db, project_id, asset_id, key):
    """Resolve an ambiguous commit with a fresh session before deleting a file."""
    try:
        db.rollback()
    except Exception:
        # An invalidated original connection never proves the file is unclaimed.
        pass
    try:
        with request.app.state.sessions() as fresh:
            # Upload commits take the same project lock. Wait for an uncertain
            # COMMIT to resolve before deciding that its asset row is absent.
            fresh.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
            if fresh.get(MediaAsset, asset_id) is not None:
                return "retained_committed"
            removed = remove_uncommitted_reference(request.app.state.settings, key)
            return "removed" if removed else "absent"
    except Exception:
        logger.warning(
            "Reference cleanup deferred for asset %s in project %s; operator reconciliation required",
            asset_id,
            project_id,
        )
        return "deferred"


@router.post("/projects/{project_id}/imports/docx", status_code=201)
async def import_docx(
    project_id: UUID, request: Request, user: User = Depends(editor), db: Session = Depends(get_db)
):
    project = project_or_404(db, project_id, user, active=True)
    db.commit()
    try:
        async with request.form(max_files=1, max_fields=0, max_part_size=4096) as form:
            file = form.get("file")
            if not isinstance(file, UploadFile) or list(form.keys()) != ["file"]:
                raise HTTPException(422, "A single DOCX file is required")
            data = await file.read(MAX_UPLOAD_BYTES + 1)
            parsed = await run_in_threadpool(parse_docx, data, file.filename, file.content_type)
        from app.storyboard import save_input

        row = save_input(
            db,
            project,
            user,
            parsed.text,
            "docx",
            parsed.source_name,
            parsed.sha256,
            parsed.warnings,
        )
        return {
            "id": str(row.id),
            "kind": row.kind,
            "text": row.text,
            "source_name": row.source_name,
            "sha256": row.sha256,
            "warnings": row.warnings,
            "created_at": row.created_at,
        }
    except IngestionError as exc:
        raise HTTPException(exc.status, exc.code) from None
    except GenerationError as exc:
        raise HTTPException(exc.status, exc.code) from None


@router.get("/projects/{project_id}/assets/reference-images")
def references(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_or_404(db, project_id, user)
    rows = db.scalars(
        select(MediaAsset)
        .where(
            MediaAsset.project_id == project_id,
            MediaAsset.kind == "reference_image",
            MediaAsset.status == "ready",
        )
        .order_by(MediaAsset.created_at.desc())
        .limit(100)
    ).all()
    return [reference_projection(row) for row in rows]


@router.post("/projects/{project_id}/assets/reference-images", status_code=201)
async def upload_reference(
    project_id: UUID, request: Request, user: User = Depends(editor), db: Session = Depends(get_db)
):
    project_or_404(db, project_id, user, active=True)
    db.commit()
    try:
        async with request.form(max_files=1, max_fields=1, max_part_size=4096) as form:
            file = form.get("file")
            if not isinstance(file, UploadFile) or set(form.keys()) - {"file", "caption"}:
                raise HTTPException(422, "A single reference image is required")
            caption = form.get("caption", "")
            if not isinstance(caption, str) or len(caption) > 500:
                raise HTTPException(422, "Reference caption must be at most 500 characters")
            data = await file.read(MAX_UPLOAD_BYTES + 1)
            source_name = safe_source_name(file.filename, "reference")
            async with request.app.state.image_slots:
                image = await run_in_threadpool(
                    canonicalize_reference, data, file.filename, file.content_type
                )
        asset_id = uuid4()
        key = f"{project_id}/references/{asset_id}.{image.extension}"
        try:
            stored_key = await run_in_threadpool(
                store_reference, image, project_id, asset_id, request.app.state.settings
            )
            if stored_key != key:
                raise StorageError("invalid_reference_key")
            project_or_404(db, project_id, user, lock=True, active=True)
            asset = MediaAsset(
                id=asset_id,
                project_id=project_id,
                uploaded_by_id=user.id,
                kind="reference_image",
                object_key=key,
                original_filename=source_name,
                mime_type=image.mime_type,
                size_bytes=len(image.data),
                sha256=image.sha256,
                storage_backend="local",
                status="ready",
                metadata_json={
                    "width": image.width,
                    "height": image.height,
                    "caption": caption,
                    "canonical_format": image.extension,
                    "original_sha256": hashlib.sha256(data).hexdigest(),
                },
            )
            db.add(asset)
            db.add(
                AuditEvent(
                    actor_user_id=user.id,
                    project_id=project_id,
                    event_type="reference.uploaded",
                    entity_type="media_asset",
                    entity_id=asset_id,
                    details={},
                )
            )
            db.commit()
        except Exception:
            _cleanup_failed_reference(request, db, project_id, asset_id, key)
            raise
        return reference_projection(asset)
    except IngestionError as exc:
        raise HTTPException(exc.status, exc.code) from None
    except StorageError as exc:
        raise HTTPException(409 if exc.retryable else 413, exc.code) from None
