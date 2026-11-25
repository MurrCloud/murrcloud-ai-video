"""Production controls publish local jobs only; no provider calls in handlers."""

import hashlib
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.dependencies import current_user, editor, get_db
from app.generation import GenerationError, owned_project, require_open_project
from app.ingestion import MAX_UPLOAD_BYTES, safe_source_name
from app.models import AuditEvent, MediaAsset, User
from app.production import (
    create_render,
    dispatch_render,
    owned_render,
    production_state,
    render_response,
    render_rows,
    save_settings,
)
from app.production_schemas import RenderIn, SettingsIn
from app.production_storage import artifact_key, publish_file
from app.storage import StorageError

router = APIRouter(prefix="/api/v1", tags=["production"])


def fail(exc):
    raise HTTPException(exc.status, exc.code) from None


@router.get("/production-config")
def config(request: Request, user: User = Depends(current_user)):
    from app.tts import voices_catalog

    settings = request.app.state.settings
    return {
        "render_enabled": settings.render_enabled,
        "narration_enabled": settings.narration_enabled,
        "tts_provider": "edge",
        "voices": [{**v, "display_name": v.get("name", v["id"])} for v in voices_catalog()],
        "max_music_upload_bytes": MAX_UPLOAD_BYTES,
    }


@router.get("/projects/{project_id}/production")
def get_production(
    project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    try:
        return production_state(db, owned_project(db, project_id, user))
    except GenerationError as exc:
        fail(exc)


@router.put("/projects/{project_id}/production/settings")
def settings_update(
    project_id: UUID,
    payload: SettingsIn,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        return production_state(db, save_settings(db, project_id, user, payload))
    except GenerationError as exc:
        fail(exc)


@router.get("/projects/{project_id}/renders")
def list_renders(
    project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    try:
        project = owned_project(db, project_id, user)
        return [render_response(db, job, project) for job in render_rows(db, project)]
    except GenerationError as exc:
        fail(exc)


@router.post("/projects/{project_id}/renders", status_code=202)
def render(
    project_id: UUID,
    payload: RenderIn,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        job = create_render(db, project_id, user, payload, request.app.state.settings)
        dispatch_render(
            db, job.id, request.app.state.settings, request.app.state.production_dispatcher
        )
        return render_response(db, job, owned_project(db, project_id, user))
    except GenerationError as exc:
        fail(exc)


@router.post("/projects/{project_id}/renders/{job_id}/dispatch", status_code=202)
def dispatch(
    project_id: UUID,
    job_id: UUID,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        project, job = owned_render(db, project_id, job_id, user)
        dispatch_render(
            db, job.id, request.app.state.settings, request.app.state.production_dispatcher
        )
        return render_response(db, job, project)
    except GenerationError as exc:
        fail(exc)


@router.post("/projects/{project_id}/renders/{job_id}/cancel", status_code=202)
def cancel(
    project_id: UUID, job_id: UUID, user: User = Depends(editor), db: Session = Depends(get_db)
):
    try:
        project, job = owned_render(db, project_id, job_id, user, lock=True)
        if job.status in {"pending", "paused", "admitted", "running"}:
            job.status = "cancelled"
            job.dispatch_state = "not_needed"
            db.add(
                AuditEvent(
                    actor_user_id=user.id,
                    project_id=project.id,
                    event_type="render.cancelled",
                    entity_type="rendering_job",
                    entity_id=job.id,
                    details={},
                )
            )
            db.commit()
        return render_response(db, job, project)
    except GenerationError as exc:
        fail(exc)


@router.post("/projects/{project_id}/assets/music", status_code=201)
async def music(
    project_id: UUID, request: Request, user: User = Depends(editor), db: Session = Depends(get_db)
):
    try:
        require_open_project(owned_project(db, project_id, user))
        db.commit()
        async with request.form(max_files=1, max_fields=0, max_part_size=4096) as form:
            file = form.get("file")
            if not isinstance(file, UploadFile) or list(form.keys()) != ["file"]:
                raise HTTPException(422, "A single MP3 or WAV file is required")
            data = await file.read(MAX_UPLOAD_BYTES + 1)
            if not data or len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "music_upload_size_rejected")
            name = safe_source_name(file.filename, "music")
            extension = Path(name).suffix.lower().lstrip(".")
            expected = {
                "mp3": {"audio/mpeg", "audio/mp3"},
                "wav": {"audio/wav", "audio/x-wav", "audio/wave"},
            }
            if extension not in expected or file.content_type not in expected[extension]:
                raise HTTPException(415, "music_type_rejected")
            if not (
                (extension == "wav" and data[:4] == b"RIFF" and data[8:12] == b"WAVE")
                or (
                    extension == "mp3"
                    and (data[:3] == b"ID3" or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"})
                )
            ):
                raise HTTPException(415, "music_signature_rejected")
        # Parsing/reading can yield while the project is archived elsewhere.
        # Admit the durable upload intent under the same project lock as archive.
        require_open_project(owned_project(db, project_id, user, lock=True))
        identifier = uuid4()
        asset = MediaAsset(
            id=identifier,
            project_id=project_id,
            uploaded_by_id=user.id,
            kind="music",
            object_key=artifact_key(project_id, identifier, extension),
            original_filename=name,
            mime_type="audio/mpeg" if extension == "mp3" else "audio/wav",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            status="pending",
            metadata_json={"probe_required": True},
        )
        db.add(asset)
        db.commit()  # Durable pending row records any interrupted publication.
        with tempfile.TemporaryDirectory(prefix="video-music-") as folder:
            source = Path(folder) / f"source.{extension}"
            source.write_bytes(data)
            await run_in_threadpool(
                publish_file,
                request.app.state.settings,
                source,
                project_id,
                identifier,
                extension,
                MAX_UPLOAD_BYTES,
            )
        asset.status = "ready"
        db.add(
            AuditEvent(
                actor_user_id=user.id,
                project_id=project_id,
                event_type="music.uploaded",
                entity_type="media_asset",
                entity_id=identifier,
                details={},
            )
        )
        db.commit()
        return {"id": str(identifier), "original_filename": name}
    except GenerationError as exc:
        fail(exc)
    except StorageError as exc:
        raise HTTPException(409 if exc.retryable else 413, exc.code) from None
