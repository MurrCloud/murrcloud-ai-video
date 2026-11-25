"""Versioned planning, owned references and atomic whole-plan spending approval."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from uuid import UUID

from sqlalchemy import func, select

from app.accounting import spending_exposure
from app.catalog import serialize_model
from app.generation import (
    MONEY_UNIT,
    TERMINAL,
    GenerationError,
    approve_estimate,
    create_estimate,
    fingerprint,
    job_response,
    json_value,
    owned_project,
    paid_gate,
    require_open_project,
)
from app.generation_schemas import GenerationApprovalIn, GenerationEstimateIn
from app.models import (
    ActualUsageCost,
    AuditEvent,
    CostEstimate,
    DirectorJob,
    GenerationJob,
    MediaAsset,
    ProjectInput,
    ProjectSettings,
    Scene,
    SceneReferenceAsset,
    SceneVersion,
    Storyboard,
    StoryboardEstimate,
    StoryboardRevision,
    VideoModel,
)
from app.providers.base import ProviderError, VideoRequest
from app.security import aware_utc
from app.storyboard_schemas import StoryboardPut

DIRECTOR_UNRESOLVED = {"approved", "submitting", "reconciliation_required"}


def lock_project(db, project_id, user):
    project = owned_project(db, project_id, user, lock=True)
    db.refresh(project)
    return require_open_project(project)


def ensure_editable(db, project_id, *, ignore_director_id=None):
    if db.scalar(
        select(GenerationJob.id)
        .where(GenerationJob.project_id == project_id, GenerationJob.status.not_in(TERMINAL))
        .limit(1)
    ):
        raise GenerationError("unresolved_paid_jobs")
    query = select(DirectorJob.id).where(
        DirectorJob.project_id == project_id, DirectorJob.status.in_(DIRECTOR_UNRESOLVED)
    )
    if ignore_director_id is not None:
        query = query.where(DirectorJob.id != ignore_director_id)
    if db.scalar(query.limit(1)):
        raise GenerationError("unresolved_paid_jobs")


def board_row(db, project, *, create=False):
    row = db.scalar(
        select(Storyboard)
        .where(Storyboard.project_id == project.id)
        .execution_options(populate_existing=True)
    )
    if row is None and create:
        row = Storyboard(
            project_id=project.id, title=project.name, revision=0, settings_snapshot={}
        )
        db.add(row)
        db.flush()
    return row


def require_revision(board, expected):
    if (board.revision if board else 0) != expected:
        raise GenerationError("revision_conflict")


def board_settings(db, project, board=None):
    settings = db.scalar(select(ProjectSettings).where(ProjectSettings.project_id == project.id))
    return {
        "target_duration_seconds": project.target_duration_seconds,
        "language": settings.language,
        "visual_style": settings.visual_style,
        "target_audience": (settings.composition or {}).get("target_audience", ""),
        "default_model_id": settings.default_model_id,
    }


def scene_data(db, scene):
    options = scene.generation_settings or {}
    model = db.scalar(select(VideoModel).where(VideoModel.model_id == scene.video_model_id))
    always_audio = model and (model.pricing_details or {}).get("audio_control") == "always_on"
    return {
        "id": scene.id,
        "scene_id": scene.scene_key,
        "order": scene.order_index,
        "duration_seconds": scene.duration_seconds,
        "generation_mode": scene.generation_mode,
        "video_model_id": scene.video_model_id,
        "resolution": options.get("resolution", "720p" if always_audio else "1080p"),
        "aspect_ratio": options.get("aspect_ratio", "16:9"),
        "audio_enabled": options.get("audio_enabled", bool(always_audio)),
        "visual_prompt": scene.visual_prompt,
        "negative_prompt": scene.negative_prompt,
        "narration": scene.narration,
        "reference_asset_ids": scene.reference_asset_ids or [],
        "transition": scene.transition,
        "status": "draft",
    }


def read_board(db, project, board=None):
    board = board if board is not None else board_row(db, project)
    scenes = db.scalars(
        select(Scene)
        .where(Scene.project_id == project.id, Scene.archived.is_(False))
        .order_by(Scene.order_index)
    ).all()
    return {
        "project_id": project.id,
        "revision": board.revision if board else 0,
        "status": board.status if board else "draft",
        "input_id": board.input_id if board else None,
        "title": board.title if board else project.name,
        **board_settings(db, project, board),
        "scenes": [scene_data(db, scene) for scene in scenes],
    }


def board_binding(db, project, board):
    value = read_board(db, project, board)
    value.pop("status", None)
    return fingerprint(value)


def record_revision(db, project, board, user):
    db.flush()
    db.add(
        StoryboardRevision(
            storyboard_id=board.id,
            revision=board.revision,
            created_by_id=user.id,
            snapshot=json_value(read_board(db, project, board)),
        )
    )


def owned_references(db, project_id, identifiers):
    identifiers = [UUID(str(identifier)) for identifier in identifiers]
    if len(identifiers) != len(set(identifiers)):
        raise GenerationError("duplicate_reference_asset", 422)
    result = []
    for identifier in identifiers:
        row = db.get(MediaAsset, identifier)
        if (
            not row
            or row.project_id != project_id
            or row.kind != "reference_image"
            or row.status != "ready"
            or row.storage_backend != "local"
        ):
            raise GenerationError("reference_asset_not_found", 404)
        result.append(row)
    return result


def save_input(db, project, user, text, kind, source_name=None, sha256=None, warnings=None):
    project = lock_project(db, project.id if hasattr(project, "id") else project, user)
    ensure_editable(db, project.id)
    if (
        kind not in {"prompt", "script", "docx"}
        or not isinstance(text, str)
        or not text.strip()
        or len(text) > 100000
        or "\x00" in text
    ):
        raise GenerationError("invalid_project_input", 422)
    if source_name is not None and (
        len(source_name) > 255 or any(character in source_name for character in ("/", "\\", "\x00"))
    ):
        raise GenerationError("invalid_source_name", 422)
    digest = sha256 or hashlib.sha256(text.encode()).hexdigest()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise GenerationError("invalid_source_digest", 422)
    warnings = warnings or []
    if (
        not isinstance(warnings, list)
        or len(warnings) > 30
        or any(not isinstance(value, str) or len(value) > 100 for value in warnings)
    ):
        raise GenerationError("invalid_input_warnings", 422)
    row = ProjectInput(
        project_id=project.id,
        created_by_id=user.id,
        kind=kind,
        text=text,
        source_name=source_name,
        sha256=digest,
        warnings=warnings,
    )
    db.add(row)
    db.flush()
    board = board_row(db, project, create=True)
    board.input_id = row.id
    board.revision += 1
    board.status = "draft"
    project.status = "draft"
    record_revision(db, project, board, user)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            project_id=project.id,
            event_type="storyboard.input_saved",
            entity_type="project_input",
            entity_id=row.id,
            details={"kind": kind, "revision": board.revision},
        )
    )
    db.commit()
    return row


def save_board(db, project_id, user, payload, *, commit=True, ignore_director_id=None):
    project = lock_project(db, project_id, user)
    ensure_editable(db, project_id, ignore_director_id=ignore_director_id)
    board = board_row(db, project, create=True)
    require_revision(board, payload.expected_revision)
    if (
        sum(
            len(scene.visual_prompt) + len(scene.negative_prompt) + len(scene.narration)
            for scene in payload.scenes
        )
        > 100000
    ):
        raise GenerationError("storyboard_text_limit", 422)
    if [scene.order for scene in payload.scenes] != list(range(1, len(payload.scenes) + 1)):
        raise GenerationError("scene_order_must_be_contiguous", 422)
    ids = [scene.id for scene in payload.scenes if scene.id is not None]
    keys = [scene.scene_id for scene in payload.scenes]
    if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
        raise GenerationError("duplicate_scene_identity", 422)
    previous = db.scalars(select(Scene).where(Scene.project_id == project_id)).all()
    by_id = {scene.id: scene for scene in previous}
    by_key = {scene.scene_key: scene for scene in previous}
    if payload.default_model_id and not db.scalar(
        select(VideoModel.id).where(VideoModel.model_id == payload.default_model_id)
    ):
        raise GenerationError("model_not_found", 422)
    for scene in payload.scenes:
        if scene.id and scene.id not in by_id:
            raise GenerationError("scene_not_found", 404)
        if scene.id and by_id[scene.id].scene_key != scene.scene_id:
            raise GenerationError("scene_key_immutable", 422)
        if not scene.id and scene.scene_id in by_key:
            raise GenerationError("scene_key_exists", 422)
        if not db.scalar(select(VideoModel.id).where(VideoModel.model_id == scene.video_model_id)):
            raise GenerationError("model_not_found", 422)
        owned_references(db, project_id, scene.reference_asset_ids)
    # Move every existing row to a disjoint positive range before reorder/reuse.
    high = (
        max((scene.order_index for scene in previous), default=0)
        + len(previous)
        + len(payload.scenes)
        + 1
    )
    for index, scene in enumerate(previous):
        scene.order_index = high + index
    db.flush()
    retained = set()
    for value in payload.scenes:
        scene = by_id.get(value.id)
        if scene is None:
            scene = Scene(project_id=project_id, scene_key=value.scene_id)
            db.add(scene)
        scene.order_index = value.order
        scene.duration_seconds = value.duration_seconds
        scene.generation_mode = value.generation_mode
        scene.video_model_id = value.video_model_id
        scene.visual_prompt = value.visual_prompt
        scene.negative_prompt = value.negative_prompt
        scene.narration = value.narration
        scene.transition = value.transition
        scene.status = "draft"
        scene.archived = False
        scene.generation_settings = {
            "resolution": value.resolution,
            "aspect_ratio": value.aspect_ratio,
            "audio_enabled": value.audio_enabled,
        }
        scene.reference_asset_ids = [str(identifier) for identifier in value.reference_asset_ids]
        db.flush()
        retained.add(scene.id)
        number = (
            db.scalar(
                select(func.max(SceneVersion.version_number)).where(
                    SceneVersion.scene_id == scene.id
                )
            )
            or 0
        ) + 1
        version = SceneVersion(
            scene_id=scene.id,
            version_number=number,
            created_by_id=user.id,
            video_model_id=scene.video_model_id,
            generation_mode=scene.generation_mode,
            duration_seconds=scene.duration_seconds,
            visual_prompt=scene.visual_prompt,
            negative_prompt=scene.negative_prompt,
            narration=scene.narration,
            settings_snapshot={
                "generation_settings": scene.generation_settings,
                "reference_asset_ids": scene.reference_asset_ids,
                "order": scene.order_index,
                "transition": scene.transition,
                "board_revision": board.revision + 1,
            },
            status="draft",
        )
        db.add(version)
        db.flush()
        for identifier in value.reference_asset_ids:
            db.add(SceneReferenceAsset(scene_version_id=version.id, asset_id=identifier))
    archived = [scene for scene in previous if scene.id not in retained]
    for index, scene in enumerate(archived, start=len(payload.scenes) + 1):
        scene.archived = True
        scene.order_index = index
    settings = db.scalar(select(ProjectSettings).where(ProjectSettings.project_id == project_id))
    project.target_duration_seconds = payload.target_duration_seconds
    settings.language = payload.language
    settings.visual_style = payload.visual_style
    settings.default_model_id = payload.default_model_id
    settings.composition = {
        **(settings.composition or {}),
        "target_audience": payload.target_audience,
    }
    board.title = payload.title
    board.settings_snapshot = {
        key: getattr(payload, key)
        for key in (
            "target_duration_seconds",
            "language",
            "visual_style",
            "target_audience",
            "default_model_id",
        )
    }
    board.revision += 1
    board.status = "draft"
    project.status = "draft"
    record_revision(db, project, board, user)
    if commit:
        db.commit()
    return board


def scene_request(value):
    # Planning references never become external URLs or fal inputs in Phase 3.
    return VideoRequest(
        model_id=value.video_model_id,
        prompt=value.visual_prompt,
        negative_prompt=value.negative_prompt,
        duration_seconds=value.duration_seconds,
        mode=value.generation_mode,
        resolution=value.resolution,
        aspect_ratio=value.aspect_ratio,
        audio_enabled=value.audio_enabled,
    )


async def validate_payload(db, project, payload, settings, provider):
    issues = []
    if not payload.scenes:
        issues.append({"scene_id": None, "code": "no_scenes"})
    if len(payload.scenes) > settings.storyboard_max_scenes:
        issues.append({"scene_id": None, "code": "too_many_scenes"})
    total = sum(scene.duration_seconds for scene in payload.scenes)
    if (
        abs(total - payload.target_duration_seconds)
        > settings.storyboard_duration_tolerance_seconds
    ):
        issues.append({"scene_id": None, "code": "duration_outside_tolerance"})
    for scene in payload.scenes:
        if not scene.visual_prompt.strip():
            issues.append({"scene_id": scene.scene_id, "code": "empty_prompt"})
            continue
        if scene.generation_mode != "text_to_video":
            issues.append({"scene_id": scene.scene_id, "code": "image_to_video_not_enabled"})
            continue
        try:
            from app.generation import current_model, restore_model, snapshot_model

            model = current_model(db, scene.video_model_id)
            owned_references(db, project.id, scene.reference_asset_ids)
            await provider.validate_request(
                restore_model(snapshot_model(model)), scene_request(scene)
            )
        except (GenerationError, ProviderError) as exc:
            issues.append({"scene_id": scene.scene_id, "code": exc.code})
        except ValueError:
            issues.append({"scene_id": scene.scene_id, "code": "invalid_scene_request"})
    return {
        "valid": not issues,
        "revision": payload.expected_revision,
        "total_duration_seconds": total,
        "target_duration_seconds": payload.target_duration_seconds,
        "tolerance_seconds": settings.storyboard_duration_tolerance_seconds,
        "issues": issues,
    }


def editor_payload(db, project, board):
    value = read_board(db, project, board)
    return StoryboardPut.model_validate(
        {
            key: value[key]
            for key in (
                "title",
                "target_duration_seconds",
                "language",
                "visual_style",
                "target_audience",
                "default_model_id",
                "scenes",
            )
        }
        | {"expected_revision": value["revision"]}
    )


async def validate_board(db, project_id, user, expected_revision, settings, provider):
    project = lock_project(db, project_id, user)
    board = board_row(db, project, create=True)
    require_revision(board, expected_revision)
    result = await validate_payload(
        db, project, editor_payload(db, project, board), settings, provider
    )
    if board.status != "approved":
        board.status = "validated" if result["valid"] else "draft"
    db.commit()
    return result


async def estimate_board(db, project_id, user, payload, settings, provider):
    project = lock_project(db, project_id, user)
    ensure_editable(db, project_id)
    board = board_row(db, project, create=True)
    require_revision(board, payload.expected_revision)
    editor = editor_payload(db, project, board)
    validation = await validate_payload(db, project, editor, settings, provider)
    if not validation["valid"]:
        raise GenerationError("storyboard_validation_required", 422)
    quotes = []
    children = []
    for scene in editor.scenes:
        quote = await create_estimate(
            db,
            project_id,
            user,
            GenerationEstimateIn(
                **scene_request(scene).model_dump(),
                scene_id=scene.id,
                retry_allowance_percent=payload.retry_allowance_percent,
            ),
            settings,
            provider,
            commit=False,
        )
        children.append(quote)
        quotes.append(
            {
                "scene_id": str(scene.id),
                "scene_key": scene.scene_id,
                "estimate_id": str(quote.id),
                "base_usd": str(quote.base_usd) if quote.base_usd is not None else None,
                "total_usd": str(quote.total_usd) if quote.total_usd is not None else None,
                "pricing_verified": quote.pricing_verified,
                "source": quote.pricing_source,
            }
        )
    snapshot = {"board_hash": board_binding(db, project, board), "scenes": quotes}
    row = StoryboardEstimate(
        project_id=project_id,
        storyboard_id=board.id,
        revision=board.revision,
        base_usd=sum((quote.base_usd for quote in children), Decimal("0"))
        if all(quote.base_usd is not None for quote in children)
        else None,
        total_usd=sum((quote.total_usd for quote in children), Decimal("0"))
        if all(quote.total_usd is not None for quote in children)
        else None,
        pricing_verified=all(quote.pricing_verified for quote in children),
        retry_allowance_percent=payload.retry_allowance_percent,
        snapshot=snapshot,
        expires_at=min(quote.expires_at for quote in children),
        plan_hash="",
    )
    row.plan_hash = board_estimate_hash(row)
    db.add(row)
    board.status = "validated"
    db.commit()
    return row


def estimate_response(row):
    return {
        "id": row.id,
        "revision": row.revision,
        "base_usd": row.base_usd,
        "total_usd": row.total_usd,
        "pricing_verified": row.pricing_verified,
        "retry_allowance_percent": row.retry_allowance_percent,
        "expires_at": row.expires_at,
        "scenes": row.snapshot["scenes"],
    }


def board_estimate_hash(row):
    return fingerprint(
        {
            "project_id": row.project_id,
            "storyboard_id": row.storyboard_id,
            "snapshot": row.snapshot,
            "revision": row.revision,
            "base_usd": row.base_usd,
            "total_usd": row.total_usd,
            "pricing_verified": row.pricing_verified,
            "retry_allowance_percent": row.retry_allowance_percent,
            "expires_at": row.expires_at,
        }
    )


def _allocations(children, remaining):
    minimums = [
        child.total_usd if child.total_usd is not None and child.total_usd > 0 else MONEY_UNIT
        for child in children
    ]
    if sum(minimums) > remaining:
        raise GenerationError("insufficient_approved_budget")
    uncertain = [index for index, child in enumerate(children) if not child.pricing_verified]
    result = list(minimums)
    if uncertain:
        surplus = remaining - sum(minimums)
        weight_total = sum(minimums[index] for index in uncertain)
        distributed = Decimal("0")
        for index in uncertain[:-1]:
            amount = (surplus * minimums[index] / weight_total).quantize(
                MONEY_UNIT, rounding=ROUND_DOWN
            )
            result[index] += amount
            distributed += amount
        result[uncertain[-1]] += surplus - distributed
    return result


def approve_board(db, project_id, user, payload, settings):
    paid_gate(settings)
    project = lock_project(db, project_id, user)
    board = board_row(db, project, create=True)
    require_revision(board, payload.expected_revision)
    row = db.scalar(
        select(StoryboardEstimate)
        .where(
            StoryboardEstimate.id == payload.estimate_id,
            StoryboardEstimate.project_id == project_id,
        )
        .with_for_update()
    )
    if row is None:
        raise GenerationError("estimate_not_found", 404)
    approval_hash = fingerprint(payload.model_dump(mode="json"))
    existing_key = db.scalar(
        select(StoryboardEstimate.id).where(
            StoryboardEstimate.approval_key == payload.idempotency_key
        )
    )
    if existing_key is not None and existing_key != row.id:
        raise GenerationError("idempotency_conflict")
    if row.status == "approved":
        if row.approval_key != payload.idempotency_key or row.approval_fingerprint != approval_hash:
            raise GenerationError("estimate_already_consumed")
        return row
    ensure_editable(db, project_id)
    if row.revision != board.revision or row.snapshot["board_hash"] != board_binding(
        db, project, board
    ):
        raise GenerationError("board_changed_since_estimate")
    if aware_utc(row.expires_at) <= datetime.now(UTC):
        raise GenerationError("estimate_expired")
    if row.plan_hash != board_estimate_hash(row):
        raise GenerationError("estimate_changed")
    exposure = spending_exposure(db, project)
    if payload.approved_cap_usd > project.budget_usd or payload.approved_cap_usd <= exposure:
        raise GenerationError("approval_cap_outside_budget")
    children = [
        db.get(CostEstimate, UUID(value["estimate_id"])) for value in row.snapshot["scenes"]
    ]
    if any(child is None or child.project_id != project_id for child in children):
        raise GenerationError("estimate_changed")
    allocations = _allocations(children, payload.approved_cap_usd - exposure)
    jobs = []
    try:
        for index, (child, allocation) in enumerate(zip(children, allocations, strict=True)):
            job = approve_estimate(
                db,
                project_id,
                user,
                GenerationApprovalIn(
                    estimate_id=child.id,
                    idempotency_key=f"board:{row.id}:{index}",
                    approved_cap_usd=spending_exposure(db, project) + allocation,
                    approve_unverified_price=payload.approve_unverified_price,
                ),
                settings,
                commit=False,
            )
            jobs.append(job)
        project.approved_cap_usd = payload.approved_cap_usd
        board.status = "approved"
        row.status = "approved"
        row.approval_key = payload.idempotency_key
        row.approval_fingerprint = approval_hash
        row.approved_cap_usd = payload.approved_cap_usd
        row.approved_by_id = user.id
        row.approved_at = datetime.now(UTC)
        row.generation_job_ids = [str(job.id) for job in jobs]
        db.commit()
    except Exception:
        db.rollback()
        raise
    return row


def approval_response(db, row):
    from app.models import Project

    project = db.get(Project, row.project_id)
    return {
        "estimate_id": row.id,
        "revision": row.revision,
        "status": row.status,
        "jobs": [
            job_response(db, db.get(GenerationJob, UUID(identifier)))
            for identifier in row.generation_job_ids
        ],
        "committed_cost_usd": project.committed_cost_usd,
        "approved_cap_usd": project.approved_cap_usd,
    }


def director_gate(settings):
    if not settings.director_enabled:
        raise GenerationError("director_disabled")
    if (
        not settings.openai_key.get_secret_value().strip()
        or not settings.director_model.strip()
        or not settings.director_approval_reference.strip()
    ):
        raise GenerationError("director_configuration_missing")


def director_context(db, project, board, source, model, request_snapshot):
    return fingerprint(
        {
            "board": board_binding(db, project, board),
            "input_id": source.id,
            "input_sha256": source.sha256,
            "text_sha256": hashlib.sha256(source.text.encode()).hexdigest(),
            "director_model": model,
            "request": request_snapshot,
        }
    )


def create_director_job(db, project_id, user, payload, settings):
    director_gate(settings)
    project = lock_project(db, project_id, user)
    approval_hash = fingerprint(payload.model_dump(mode="json"))
    existing = db.scalar(
        select(DirectorJob).where(DirectorJob.idempotency_key == payload.idempotency_key)
    )
    if existing:
        if existing.project_id != project_id or existing.approval_fingerprint != approval_hash:
            raise GenerationError("idempotency_conflict")
        return existing
    ensure_editable(db, project_id)
    board = board_row(db, project, create=True)
    require_revision(board, payload.expected_revision)
    source = db.get(ProjectInput, payload.input_id)
    if not source or source.project_id != project_id or board.input_id != source.id:
        raise GenerationError("current_input_required")
    if not payload.approve_unknown_price:
        raise GenerationError("explicit_unknown_price_approval_required")
    exposure = spending_exposure(db, project)
    cap = payload.approved_cap_usd or max(
        project.approved_cap_usd or Decimal("0"), exposure + payload.max_cost_usd
    )
    reserve = payload.max_cost_usd if payload.max_cost_usd is not None else cap - exposure
    if cap > project.budget_usd or reserve <= 0 or exposure + reserve > cap:
        raise GenerationError("approval_cap_outside_budget")
    values = board_settings(db, project, board)
    if not values["default_model_id"]:
        raise GenerationError("default_model_required", 422)
    models = db.scalars(
        select(VideoModel)
        .where(VideoModel.enabled.is_(True), VideoModel.verified_at.is_not(None))
        .order_by(VideoModel.model_id)
        .limit(120)
    ).all()
    catalog = [
        serialize_model(model)
        for model in models
        if "text_to_video" in model.supported_generation_modes
    ]
    if values["default_model_id"] not in {model["model_id"] for model in catalog}:
        raise GenerationError("default_model_not_enabled", 422)
    references = db.scalars(
        select(MediaAsset)
        .where(
            MediaAsset.project_id == project_id,
            MediaAsset.kind == "reference_image",
            MediaAsset.status == "ready",
            MediaAsset.storage_backend == "local",
            MediaAsset.sha256.is_not(None),
        )
        .order_by(MediaAsset.created_at, MediaAsset.id)
        .limit(120)
    ).all()
    reference_data = [
        {
            "id": str(asset.id),
            "mime_type": asset.mime_type,
            "width": asset.metadata_json.get("width"),
            "height": asset.metadata_json.get("height"),
            "caption": str(asset.metadata_json.get("caption", ""))[:500],
            "sha256": asset.sha256,
        }
        for asset in references
    ]
    snapshot = json_value(
        {
            "script": source.text,
            **values,
            "budget_usd": project.budget_usd - exposure - reserve,
            "models": catalog,
            "reference_assets": reference_data,
        }
    )
    snapshot.pop("default_model_id", None)
    snapshot["default_model_id"] = values["default_model_id"]
    row = DirectorJob(
        project_id=project_id,
        storyboard_id=board.id,
        input_id=source.id,
        board_revision=board.revision,
        context_hash=director_context(
            db, project, board, source, settings.director_model, snapshot
        ),
        model=settings.director_model,
        request_snapshot=snapshot,
        idempotency_key=payload.idempotency_key,
        approval_fingerprint=approval_hash,
        approved_by_id=user.id,
        reserved_cost_usd=reserve,
        approved_at=datetime.now(UTC),
    )
    project.approved_cap_usd = cap
    project.committed_cost_usd += reserve
    db.add(row)
    db.flush()
    db.add(
        ActualUsageCost(
            project_id=project_id,
            director_job_id=row.id,
            provider="openai",
            amount_usd=None,
            status="unavailable",
            usage_details={"model": settings.director_model, "reservation_held": True},
        )
    )
    db.commit()
    return row


def director_response(row):
    return {
        "id": row.id,
        "input_id": row.input_id,
        "board_revision": row.board_revision,
        "status": row.status,
        "result_revision": row.result_revision,
        "reserved_cost_usd": row.reserved_cost_usd,
        "actual_cost_usd": None,
        "usage": row.usage,
        "error_code": row.error_code,
        "dispatch_state": row.dispatch_state,
        "dispatch_error_code": row.dispatch_error_code,
        "created_at": row.created_at,
    }
