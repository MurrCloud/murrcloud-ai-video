"""Owned production plans and immutable render snapshots; no media work in API calls."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.generation import (
    GenerationError,
    fingerprint,
    json_value,
    owned_project,
    require_open_project,
)
from app.models import (
    AuditEvent,
    GenerationJob,
    MediaAsset,
    ProjectSettings,
    RenderingJob,
    Scene,
    SceneVersion,
)
from app.production_schemas import ProductionSettings
from app.storyboard import board_row, record_revision, require_revision

ACTIVE = {"pending", "admitted", "running", "paused"}


def render_gate(settings, *, narration=False):
    if not settings.render_enabled:
        raise GenerationError("rendering_disabled")
    if narration and not settings.narration_enabled:
        raise GenerationError("narration_disabled")


def revision(db, project):
    board = board_row(db, project)
    return board.revision if board else 0


def settings_for(db, project):
    from app.tts import validate_voice

    row = db.scalar(select(ProjectSettings).where(ProjectSettings.project_id == project.id))
    stored = (row.composition or {}).get("production")
    if stored:
        return ProductionSettings.model_validate(stored)
    return ProductionSettings(
        aspect_ratio=row.aspect_ratio,
        language=row.language,
        narration_enabled=row.narration_enabled,
        burn_subtitles=row.narration_enabled,
        voice=validate_voice(row.language, row.voice),
        speaking_rate=row.speaking_rate,
    )


def owned_asset(db, project_id, identifier, kinds):
    asset = db.get(MediaAsset, identifier) if identifier else None
    if (
        not asset
        or asset.project_id != project_id
        or asset.kind not in kinds
        or asset.status != "ready"
        or asset.storage_backend != "local"
        or not asset.sha256
    ):
        raise GenerationError("production_asset_not_found", 404)
    return asset


def active_scenes(db, project):
    return db.scalars(
        select(Scene)
        .where(Scene.project_id == project.id, Scene.archived.is_(False))
        .order_by(Scene.order_index)
    ).all()


def source_binding(db, project):
    settings = settings_for(db, project)
    attachments = []
    for identifier, kinds in (
        (settings.music_asset_id, {"music"}),
        (settings.watermark_asset_id, {"reference_image", "logo"}),
    ):
        if identifier:
            asset = owned_asset(db, project.id, identifier, kinds)
            attachments.append({"id": str(asset.id), "sha256": asset.sha256})
    return json_value(
        {
            "project_id": project.id,
            "revision": revision(db, project),
            "target_duration_seconds": project.target_duration_seconds,
            "settings": settings.model_dump(mode="json"),
            "attachments": attachments,
            "scenes": [
                {
                    "id": scene.id,
                    "key": scene.scene_key,
                    "order": scene.order_index,
                    "duration_seconds": scene.duration_seconds,
                    "model": scene.video_model_id,
                    "mode": scene.generation_mode,
                    "prompt": scene.visual_prompt,
                    "negative_prompt": scene.negative_prompt,
                    "narration": scene.narration,
                    "transition": scene.transition,
                    "settings": scene.generation_settings,
                }
                for scene in active_scenes(db, project)
            ],
        }
    )


def clip_versions(db, project, scene):
    rows = db.execute(
        select(SceneVersion, GenerationJob, MediaAsset)
        .join(GenerationJob, GenerationJob.scene_version_id == SceneVersion.id)
        .join(MediaAsset, MediaAsset.id == GenerationJob.output_asset_id)
        .where(
            SceneVersion.scene_id == scene.id,
            GenerationJob.project_id == project.id,
            GenerationJob.status == "succeeded",
            MediaAsset.status == "ready",
            MediaAsset.kind == "clip",
            MediaAsset.project_id == project.id,
            MediaAsset.storage_backend == "local",
        )
        .order_by(SceneVersion.version_number.desc())
        .limit(20)
    ).all()
    return rows


def default_version(rows, scene):
    for version, job, _asset in rows:
        request = job.request_snapshot or {}
        if (
            version.video_model_id == scene.video_model_id
            and version.duration_seconds == scene.duration_seconds
            and version.visual_prompt == scene.visual_prompt
            and version.negative_prompt == scene.negative_prompt
            and version.generation_mode == scene.generation_mode
            and all(request.get(k) == v for k, v in (scene.generation_settings or {}).items())
        ):
            return version.id
    return None


def render_response(db, job, project):
    current = False
    try:
        current = job.composition_snapshot.get("binding_hash") == fingerprint(
            source_binding(db, project)
        )
    except (GenerationError, ValueError):
        pass
    ready = job.status == "succeeded" and job.output_asset_id is not None
    return json_value(
        {
            "id": job.id,
            "status": job.status,
            "stage": job.stage,
            "progress_percent": job.progress_percent,
            "error_code": job.error_code,
            "warnings": job.warnings,
            "is_current": current,
            "output_asset_id": job.output_asset_id if ready else None,
            "subtitle_asset_id": job.subtitle_asset_id if ready else None,
            "preview_url": f"/api/v1/assets/{job.output_asset_id}/preview" if ready else None,
            "download_url": f"/api/v1/assets/{job.output_asset_id}/download" if ready else None,
            "srt_url": f"/api/v1/assets/{job.subtitle_asset_id}/download"
            if ready and job.subtitle_asset_id
            else None,
            "dispatch_state": job.dispatch_state,
            "created_at": job.created_at,
            "validation_report": job.validation_report,
        }
    )


def render_rows(db, project):
    return db.scalars(
        select(RenderingJob)
        .where(RenderingJob.project_id == project.id)
        .order_by(RenderingJob.created_at.desc(), RenderingJob.id)
        .limit(30)
    ).all()


def production_state(db, project):
    scenes = []
    for scene in active_scenes(db, project):
        rows = clip_versions(db, project, scene)
        scenes.append(
            {
                "scene_id": scene.id,
                "scene_key": scene.scene_key,
                "order": scene.order_index,
                "duration_seconds": scene.duration_seconds,
                "narration": scene.narration,
                "selected_scene_version_id": default_version(rows, scene),
                "versions": [
                    {
                        "scene_version_id": version.id,
                        "version_number": version.version_number,
                        "clip_asset_id": asset.id,
                        "preview_url": f"/api/v1/assets/{asset.id}/preview",
                        "download_url": f"/api/v1/assets/{asset.id}/download",
                        "width": (asset.metadata_json or {}).get("width"),
                        "height": (asset.metadata_json or {}).get("height"),
                    }
                    for version, _job, asset in rows
                ],
            }
        )
    assets = db.scalars(
        select(MediaAsset)
        .where(
            MediaAsset.project_id == project.id,
            MediaAsset.status == "ready",
            MediaAsset.kind.in_(["music", "reference_image", "logo"]),
        )
        .order_by(MediaAsset.created_at.desc())
        .limit(100)
    ).all()
    return json_value(
        {
            "project_id": project.id,
            "archived": project.archived,
            "revision": revision(db, project),
            "settings": settings_for(db, project).model_dump(mode="json"),
            "scenes": scenes,
            "music_assets": [
                {"id": a.id, "original_filename": a.original_filename}
                for a in assets
                if a.kind == "music"
            ],
            "reference_assets": [
                {"id": a.id, "preview_url": f"/api/v1/assets/{a.id}/preview"}
                for a in assets
                if a.kind != "music"
            ],
            "render_jobs": [render_response(db, j, project) for j in render_rows(db, project)],
        }
    )


def save_settings(db, project_id, user, payload):
    project = require_open_project(owned_project(db, project_id, user, lock=True))
    board = board_row(db, project, create=True)
    require_revision(board, payload.expected_revision)
    for identifier, kinds in (
        (payload.settings.music_asset_id, {"music"}),
        (payload.settings.watermark_asset_id, {"reference_image", "logo"}),
    ):
        if identifier:
            owned_asset(db, project_id, identifier, kinds)
    row = db.scalar(select(ProjectSettings).where(ProjectSettings.project_id == project_id))
    row.composition = {
        **(row.composition or {}),
        "production": payload.settings.model_dump(mode="json"),
    }
    board.revision += 1
    board.status = "draft"
    project.status = "draft"
    record_revision(db, project, board, user)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            project_id=project_id,
            event_type="production.settings_saved",
            entity_type="storyboard",
            entity_id=board.id,
            details={"revision": board.revision},
        )
    )
    db.commit()
    return project


def create_render(db, project_id, user, payload, settings):
    project = owned_project(db, project_id, user, lock=True)
    body_hash = fingerprint(payload.model_dump(mode="json"))
    existing = db.scalar(
        select(RenderingJob).where(RenderingJob.idempotency_key == payload.idempotency_key)
    )
    if existing:
        if existing.project_id != project_id or existing.request_fingerprint != body_hash:
            raise GenerationError("idempotency_conflict")
        return existing
    require_open_project(project)
    require_revision(board_row(db, project), payload.expected_revision)
    options = settings_for(db, project)
    render_gate(settings, narration=options.narration_enabled)
    if db.scalar(
        select(RenderingJob.id).where(
            RenderingJob.project_id == project_id, RenderingJob.status.in_(ACTIVE)
        )
    ):
        raise GenerationError("render_already_active")
    scenes = active_scenes(db, project)
    if not scenes or len(scenes) > settings.storyboard_max_scenes:
        raise GenerationError("invalid_render_scene_count", 422)
    if (
        sum(s.duration_seconds for s in scenes) > 3600
        or abs(sum(s.duration_seconds for s in scenes) - project.target_duration_seconds)
        > settings.storyboard_duration_tolerance_seconds
    ):
        raise GenerationError("duration_outside_tolerance", 422)
    if payload.scene_versions and set(payload.scene_versions) != {s.id for s in scenes}:
        raise GenerationError("complete_scene_version_selection_required", 422)
    plan = []
    for scene in scenes:
        rows = clip_versions(db, project, scene)
        selected = payload.scene_versions.get(scene.id) or default_version(rows, scene)
        match = next((r for r in rows if r[0].id == selected), None)
        if not match:
            raise GenerationError("scene_clip_not_ready", 409)
        version, job, asset = match
        if version.duration_seconds != scene.duration_seconds or not asset.sha256:
            raise GenerationError("selected_clip_duration_mismatch", 422)
        plan.append(
            {
                "scene_id": str(scene.id),
                "scene_version_id": str(version.id),
                "clip_asset_id": str(asset.id),
                "clip_sha256": asset.sha256,
                "duration_seconds": scene.duration_seconds,
                "narration": scene.narration,
                "transition": scene.transition,
                "order": scene.order_index,
            }
        )
    binding = source_binding(db, project)
    snapshot = {
        "binding_hash": fingerprint(binding),
        "source": binding,
        "settings": options.model_dump(mode="json"),
        "scenes": plan,
        "prepared": {},
    }
    job = RenderingJob(
        project_id=project_id,
        idempotency_key=payload.idempotency_key,
        requested_by_id=user.id,
        request_fingerprint=body_hash,
        status="pending",
        stage="narration",
        composition_snapshot=snapshot,
    )
    db.add(job)
    db.flush()
    project.status = "narrating" if options.narration_enabled else "rendering"
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            project_id=project_id,
            event_type="render.requested",
            entity_type="rendering_job",
            entity_id=job.id,
            details={"revision": payload.expected_revision},
        )
    )
    db.commit()
    return job


def owned_render(db, project_id, identifier, user, *, lock=False):
    project = owned_project(db, project_id, user, lock=lock)
    query = select(RenderingJob).where(
        RenderingJob.id == identifier, RenderingJob.project_id == project_id
    )
    job = db.scalar(
        query.with_for_update().execution_options(populate_existing=True) if lock else query
    )
    if not job:
        raise GenerationError("render_not_found", 404)
    return project, job


def dispatch_render(db, identifier, settings, dispatcher, *, countdown=0):
    job = db.get(RenderingJob, identifier)
    if not job or job.status not in {"pending", "paused"}:
        return False
    render_gate(
        settings,
        narration=job.stage == "narration"
        and job.composition_snapshot.get("settings", {}).get("narration_enabled", False),
    )
    task = "video.prepare_render" if job.stage == "narration" else "video.render_video"
    db.commit()
    try:
        dispatcher(task, str(identifier), countdown=countdown)
        error = None
    except Exception:
        error = "broker_unavailable"
    db.refresh(job)
    if job.status in {"pending", "paused"}:
        job.dispatch_state = "error" if error else "queued"
        job.dispatch_error_code = error
        job.updated_at = datetime.now(UTC)
        db.commit()
    return error is None
