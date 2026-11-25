"""Durable approvals and reservations. Paid HTTP never occurs in an API transaction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.accounting import spending_exposure
from app.catalog import model_spec
from app.models import (
    ActualUsageCost,
    AuditEvent,
    CostEstimate,
    DirectorJob,
    GenerationJob,
    Project,
    ProjectSettings,
    ProviderConfiguration,
    Scene,
    SceneReferenceAsset,
    SceneVersion,
    Storyboard,
    VideoModel,
)
from app.providers.base import ModelSpec, ProviderError, VideoRequest
from app.security import aware_utc

TERMINAL = {"succeeded", "failed", "cancelled", "abandoned"}
MONEY_UNIT = Decimal("0.000001")


class GenerationError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


def safe_code(code, fallback="provider_error"):
    return code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code) else fallback


def json_value(value):
    def encode(item):
        if isinstance(item, Decimal):
            return format(item.normalize(), "f")
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime):
            return aware_utc(item).isoformat()
        raise TypeError("Unsupported snapshot value")

    return json.loads(json.dumps(value, default=encode, sort_keys=True, allow_nan=False))


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def paid_gate(settings):
    if not settings.paid_generation_enabled:
        raise GenerationError("paid_generation_disabled")
    if not settings.paid_approval_reference or not settings.fal_key.get_secret_value():
        raise GenerationError("paid_configuration_missing")


def owned_project(db, project_id, user, *, lock=False):
    query = select(Project).where(Project.id == project_id)
    if user.role != "admin":
        query = query.where(Project.owner_id == user.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    project = db.scalar(query)
    if project is None:
        raise GenerationError("project_not_found", 404)
    return project


def require_open_project(project):
    if project.archived:
        raise GenerationError("project_archived")
    return project


def current_model(db, model_id):
    model = db.scalar(select(VideoModel).where(VideoModel.model_id == model_id))
    if model is None:
        raise GenerationError("model_not_found", 422)
    provider = db.scalar(
        select(ProviderConfiguration).where(ProviderConfiguration.provider == model.provider)
    )
    if not model.enabled or not provider or not provider.enabled:
        raise GenerationError("model_or_provider_disabled", 422)
    return model


def snapshot_model(model):
    return json_value(asdict(model_spec(model)))


def restore_model(snapshot):
    values = dict(snapshot)
    for field in ("modes", "resolutions", "aspect_ratios"):
        values[field] = tuple(values[field])
    if values.get("price_usd") is not None:
        values["price_usd"] = Decimal(values["price_usd"])
    return ModelSpec(**values)


def context_snapshot(db, project_id, scene_id):
    settings = db.scalar(select(ProjectSettings).where(ProjectSettings.project_id == project_id))
    if settings is None:
        raise GenerationError("project_settings_missing")
    settings_value = {
        column.name: getattr(settings, column.name)
        for column in ProjectSettings.__table__.columns
        if column.name not in {"id", "project_id", "created_at", "updated_at"}
    }
    version = None
    if scene_id is not None:
        scene = db.scalar(
            select(Scene).where(
                Scene.id == scene_id, Scene.project_id == project_id, Scene.archived.is_(False)
            )
        )
        if scene is None:
            raise GenerationError("scene_not_found", 404)
        unresolved = db.scalar(
            select(GenerationJob.id)
            .join(SceneVersion, GenerationJob.scene_version_id == SceneVersion.id)
            .where(SceneVersion.scene_id == scene_id, GenerationJob.status.not_in(TERMINAL))
            .limit(1)
        )
        if unresolved:
            raise GenerationError("scene_generation_requires_completion_or_reconciliation")
        version = (
            db.scalar(
                select(func.max(SceneVersion.version_number)).where(
                    SceneVersion.scene_id == scene_id
                )
            )
            or 0
        )
    board = db.scalar(select(Storyboard).where(Storyboard.project_id == project_id))
    scene_fields = None
    if scene_id is not None:
        scene_fields = {
            "scene_key": scene.scene_key,
            "order": scene.order_index,
            "duration_seconds": scene.duration_seconds,
            "generation_mode": scene.generation_mode,
            "video_model_id": scene.video_model_id,
            "visual_prompt": scene.visual_prompt,
            "negative_prompt": scene.negative_prompt,
            "narration": scene.narration,
            "transition": scene.transition,
            "generation_settings": scene.generation_settings,
            "reference_asset_ids": scene.reference_asset_ids,
        }
    return json_value(
        {
            "project_settings": settings_value,
            "scene_id": scene_id,
            "scene_version_number": version,
            "scene_fields": scene_fields,
            "board_revision": board.revision if board else None,
            "board_input_id": board.input_id if board else None,
        }
    )


def estimate_hash(estimate):
    return fingerprint(
        {
            "project_id": estimate.project_id,
            "request": estimate.request_snapshot,
            "model": estimate.model_snapshot,
            "context": estimate.context_snapshot,
            "base_usd": estimate.base_usd,
            "total_usd": estimate.total_usd,
            "pricing_verified": estimate.pricing_verified,
            "pricing_source": estimate.pricing_source,
            "retry_allowance_percent": estimate.retry_allowance_percent,
            "expires_at": estimate.expires_at,
        }
    )


async def create_estimate(db, project_id, user, payload, settings, provider, *, commit=True):
    require_open_project(owned_project(db, project_id, user))
    request = VideoRequest.model_validate(
        payload.model_dump(exclude={"scene_id", "retry_allowance_percent"})
    )
    if request.mode != "text_to_video":
        raise GenerationError("phase2_text_to_video_only", 422)
    model = current_model(db, request.model_id)
    spec = model_spec(model)
    try:
        await provider.validate_request(spec, request)
        quote = await provider.estimate_cost(spec, request)
    except ProviderError as exc:
        raise GenerationError(safe_code(exc.code), 422) from None
    base = quote.amount_usd
    if base is not None:
        base = Decimal(str(base))
        if not base.is_finite() or base < 0:
            base = None
        else:
            base = base.quantize(MONEY_UNIT, rounding=ROUND_UP)
    total = (
        None
        if base is None
        else (base * (Decimal("1") + payload.retry_allowance_percent / 100)).quantize(
            MONEY_UNIT, rounding=ROUND_UP
        )
    )
    context = context_snapshot(db, project_id, payload.scene_id)
    estimate = CostEstimate(
        project_id=project_id,
        base_usd=base,
        total_usd=total,
        pricing_available=base is not None,
        pricing_verified=bool(quote.verified and base is not None),
        pricing_source=safe_code(quote.source, "unavailable"),
        request_snapshot=request.model_dump(mode="json"),
        model_snapshot=snapshot_model(model),
        context_snapshot=context,
        scene_id=payload.scene_id,
        scene_version_number=context["scene_version_number"],
        retry_allowance_percent=payload.retry_allowance_percent,
        breakdown=[
            {
                "kind": "generation",
                "base_usd": str(base) if base is not None else None,
                "basis": json_value(quote.basis),
            }
        ],
        plan_hash="",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.estimate_ttl_seconds),
    )
    estimate.plan_hash = estimate_hash(estimate)
    db.add(estimate)
    db.flush()
    if commit:
        db.commit()
    return estimate


def _record_standalone_board_generation(db, project, user, *, append_scene):
    board = db.scalar(select(Storyboard).where(Storyboard.project_id == project.id))
    if board is None:
        return
    if append_scene:
        # Appending through the legacy clip API must preserve the editor's
        # active-first contiguous ordering, including when archives exist.
        scenes = db.scalars(
            select(Scene)
            .where(Scene.project_id == project.id)
            .order_by(Scene.order_index, Scene.id)
        ).all()
        high = max((scene.order_index for scene in scenes), default=0) + len(scenes) + 1
        for index, scene in enumerate(scenes):
            scene.order_index = high + index
        db.flush()
        ordered = [scene for scene in scenes if not scene.archived] + [
            scene for scene in scenes if scene.archived
        ]
        for index, scene in enumerate(ordered, start=1):
            scene.order_index = index
    board.revision += 1
    board.status = "draft"
    # Deferred import avoids a module cycle; whole-plan approval uses
    # commit=False and deliberately never enters this standalone path.
    from app.storyboard import record_revision

    record_revision(db, project, board, user)


def approve_estimate(db, project_id, user, payload, settings, *, commit=True):
    paid_gate(settings)
    project = owned_project(db, project_id, user, lock=True)
    approval_hash = fingerprint(
        {
            "estimate_id": payload.estimate_id,
            "approved_cap_usd": payload.approved_cap_usd,
            "approve_unverified_price": payload.approve_unverified_price,
        }
    )
    existing = db.scalar(
        select(GenerationJob).where(GenerationJob.idempotency_key == payload.idempotency_key)
    )
    if existing is not None:
        if existing.project_id != project_id or existing.approval_fingerprint != approval_hash:
            raise GenerationError("idempotency_conflict")
        return existing
    require_open_project(project)
    if db.scalar(
        select(DirectorJob.id)
        .where(
            DirectorJob.project_id == project_id,
            DirectorJob.status.in_(["approved", "submitting", "reconciliation_required"]),
        )
        .limit(1)
    ):
        raise GenerationError("unresolved_director_job")
    estimate = db.scalar(
        select(CostEstimate)
        .where(CostEstimate.id == payload.estimate_id, CostEstimate.project_id == project_id)
        .with_for_update()
    )
    if estimate is None:
        raise GenerationError("estimate_not_found", 404)
    if estimate.status != "pending":
        raise GenerationError("estimate_already_consumed")
    if not estimate.expires_at or aware_utc(estimate.expires_at) <= datetime.now(UTC):
        raise GenerationError("estimate_expired")
    if estimate.plan_hash != estimate_hash(estimate):
        raise GenerationError("estimate_changed")
    request = VideoRequest.model_validate(estimate.request_snapshot)
    if snapshot_model(current_model(db, request.model_id)) != estimate.model_snapshot:
        raise GenerationError("model_changed_since_estimate")
    context = context_snapshot(db, project_id, estimate.scene_id)
    if context != estimate.context_snapshot:
        raise GenerationError("scene_or_settings_changed_since_estimate")
    cap = payload.approved_cap_usd
    exposure = spending_exposure(db, project)
    if cap > project.budget_usd or cap < exposure:
        raise GenerationError("approval_cap_outside_budget")
    remaining = cap - exposure
    if not estimate.pricing_verified and not payload.approve_unverified_price:
        raise GenerationError("explicit_unverified_price_approval_required")
    if remaining <= 0 or (estimate.total_usd is not None and estimate.total_usd > remaining):
        raise GenerationError("insufficient_approved_budget")
    reservation = estimate.total_usd if estimate.pricing_verified else remaining
    if reservation is None or reservation <= 0:
        raise GenerationError("positive_reservation_required")
    if estimate.scene_id is None:
        if commit and db.scalar(select(Storyboard.id).where(Storyboard.project_id == project_id)):
            active_count = db.scalar(
                select(func.count())
                .select_from(Scene)
                .where(Scene.project_id == project_id, Scene.archived.is_(False))
            )
            if active_count >= settings.storyboard_max_scenes:
                raise GenerationError("too_many_scenes", 422)
        scene = Scene(
            project_id=project_id,
            scene_key="scene_" + uuid4().hex,
            order_index=(
                db.scalar(select(func.max(Scene.order_index)).where(Scene.project_id == project_id))
                or 0
            )
            + 1,
            duration_seconds=request.duration_seconds,
            generation_mode=request.mode,
            video_model_id=request.model_id,
            visual_prompt=request.prompt,
            negative_prompt=request.negative_prompt,
            status="queued",
        )
        db.add(scene)
        db.flush()
        version_number = 1
    else:
        scene = db.get(Scene, estimate.scene_id)
        version_number = (estimate.scene_version_number or 0) + 1
        scene.duration_seconds = request.duration_seconds
        scene.video_model_id = request.model_id
        scene.generation_mode = request.mode
        scene.visual_prompt = request.prompt
        scene.negative_prompt = request.negative_prompt
        scene.status = "queued"
    scene.generation_settings = {
        "resolution": request.resolution,
        "aspect_ratio": request.aspect_ratio,
        "audio_enabled": request.audio_enabled,
    }
    version = SceneVersion(
        scene_id=scene.id,
        version_number=version_number,
        created_by_id=user.id,
        video_model_id=request.model_id,
        generation_mode=request.mode,
        duration_seconds=request.duration_seconds,
        visual_prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        narration=scene.narration,
        settings_snapshot={"request": request.model_dump(mode="json"), **context},
        status="queued",
    )
    db.add(version)
    db.flush()
    for identifier in scene.reference_asset_ids or []:
        from app.models import MediaAsset

        asset = db.get(MediaAsset, UUID(identifier))
        if (
            not asset
            or asset.project_id != project_id
            or asset.kind != "reference_image"
            or asset.status != "ready"
        ):
            raise GenerationError("reference_asset_not_found", 404)
        db.add(SceneReferenceAsset(scene_version_id=version.id, asset_id=asset.id))
    now = datetime.now(UTC)
    job = GenerationJob(
        project_id=project_id,
        scene_version_id=version.id,
        estimate_id=estimate.id,
        model_id=request.model_id,
        provider=estimate.model_snapshot["provider"],
        endpoint_id=estimate.model_snapshot["endpoint_id"],
        idempotency_key=payload.idempotency_key,
        status="approved",
        request_snapshot=estimate.request_snapshot,
        model_snapshot=estimate.model_snapshot,
        estimated_cost_usd=estimate.total_usd,
        reserved_cost_usd=reservation,
        approved_by_id=user.id,
        approved_at=now,
        approval_fingerprint=approval_hash,
        dispatch_state="pending",
    )
    project.approved_cap_usd = cap
    project.committed_cost_usd += reservation
    project.status = "generating"
    estimate.status = "approved"
    estimate.approved_by_id = user.id
    estimate.approved_at = now
    db.add(job)
    db.flush()
    db.add(
        ActualUsageCost(
            project_id=project_id,
            scene_version_id=version.id,
            generation_job_id=job.id,
            model_id=job.model_id,
            provider=job.provider,
            amount_usd=None,
            status="unavailable",
            usage_details={"billing": "not_reported", "reservation_held": True},
        )
    )
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            project_id=project_id,
            event_type="generation.approved",
            entity_type="generation_job",
            entity_id=job.id,
            details={
                "reserved_cost_usd": str(reservation),
                "pricing_verified": estimate.pricing_verified,
            },
        )
    )
    try:
        if commit:
            _record_standalone_board_generation(
                db, project, user, append_scene=estimate.scene_id is None
            )
            db.commit()
        else:
            db.flush()
    except IntegrityError:
        db.rollback()
        raise GenerationError("generation_conflict") from None
    return job


def project_job(db, project_id, job_id, user, *, lock=False):
    owned_project(db, project_id, user)
    query = select(GenerationJob).where(
        GenerationJob.id == job_id, GenerationJob.project_id == project_id
    )
    job = db.scalar(query.with_for_update() if lock else query)
    if job is None:
        raise GenerationError("generation_not_found", 404)
    return job


def job_response(db, job):
    version = db.get(SceneVersion, job.scene_version_id)
    estimate = db.get(CostEstimate, job.estimate_id) if job.estimate_id else None
    costs = db.scalars(
        select(ActualUsageCost).where(
            ActualUsageCost.generation_job_id == job.id,
            ActualUsageCost.status == "confirmed",
            ActualUsageCost.amount_usd.is_not(None),
        )
    ).all()
    actual = sum((row.amount_usd for row in costs), Decimal("0")) if costs else None
    return {
        "id": job.id,
        "project_id": job.project_id,
        "scene_id": version.scene_id,
        "scene_version_id": version.id,
        "version_number": version.version_number,
        "model_id": job.model_id,
        "status": job.status,
        "request": job.request_snapshot,
        "estimated_cost_usd": job.estimated_cost_usd,
        "reserved_cost_usd": job.reserved_cost_usd,
        "actual_cost_usd": actual,
        "actual_cost_status": "confirmed" if actual is not None else "unavailable",
        "pricing_verified": bool(estimate and estimate.pricing_verified),
        "no_longer_wanted": job.no_longer_wanted,
        "last_error_code": job.last_error_code,
        "dispatch_state": job.dispatch_state,
        "dispatch_error_code": job.dispatch_error_code,
        "next_poll_at": job.next_poll_at,
        "output_asset_id": job.output_asset_id,
        "download_url": f"/api/v1/assets/{job.output_asset_id}/download"
        if job.output_asset_id
        else None,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def dispatch_job(db, job_id, settings, dispatcher, *, countdown=0):
    job = db.scalar(select(GenerationJob).where(GenerationJob.id == job_id).with_for_update())
    if not job or job.status in TERMINAL:
        if job:
            job.dispatch_state = "not_needed"
            db.commit()
        return False
    if job.provider_job_id:
        action = (
            "cancel_generation"
            if job.no_longer_wanted
            and job.status == "cancellation_requested"
            and job.cancel_attempted_at is None
            else "poll_generation"
        )
    elif job.status == "approved":
        paid_gate(settings)
        action = "submit_generation"
    else:
        # An unknown outcome must never be turned into another paid POST.
        return False
    db.commit()
    try:
        dispatcher("video." + action, str(job_id), countdown=countdown)
        dispatched = True
    except Exception:
        dispatched = False
    job = db.scalar(select(GenerationJob).where(GenerationJob.id == job_id).with_for_update())
    job.dispatch_state = "queued" if dispatched else "error"
    job.dispatch_error_code = None if dispatched else "broker_unavailable"
    if dispatched:
        job.dispatched_at = datetime.now(UTC)
    db.commit()
    return dispatched


def request_cancel(db, project_id, job_id, user):
    owned_project(db, project_id, user, lock=True)
    job = project_job(db, project_id, job_id, user, lock=True)
    job.no_longer_wanted = True
    if job.status not in TERMINAL:
        if job.provider_job_id:
            job.status = "cancellation_requested"
            job.cancel_attempted_at = None
            job.next_poll_at = datetime.now(UTC)
        elif job.status == "approved":
            job.status = "cancelled"
            job.completed_at = datetime.now(UTC)
            job.dispatch_state = "not_needed"
            update_scene_state(db, job, "cancelled")
        else:
            job.status = "reconciliation_required"
            job.last_error_code = "submission_outcome_unknown"
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            project_id=project_id,
            event_type="generation.cancel_requested",
            entity_type="generation_job",
            entity_id=job.id,
            details={"reservation_held": True},
        )
    )
    db.commit()
    return job


def update_scene_state(db, job, state):
    # Worker callers lock the project before the job. Keep that boundary through
    # scene mutations and aggregate status so concurrent approvals cannot vanish.
    with db.no_autoflush:
        project = db.scalar(select(Project).where(Project.id == job.project_id).with_for_update())
    version = db.get(SceneVersion, job.scene_version_id)
    if version is None:
        return
    version.status = state
    latest = db.scalar(
        select(func.max(SceneVersion.version_number)).where(
            SceneVersion.scene_id == version.scene_id
        )
    )
    if version.version_number == latest:
        scene = db.get(Scene, version.scene_id)
        if not scene.archived:
            scene.status = state
    db.flush()
    active = db.scalar(
        select(GenerationJob.id)
        .where(GenerationJob.project_id == job.project_id, GenerationJob.status.not_in(TERMINAL))
        .limit(1)
    )
    if project is not None:
        if active:
            project.status = "generating"
        else:
            states = set(
                db.scalars(
                    select(Scene.status).where(
                        Scene.project_id == job.project_id, Scene.archived.is_(False)
                    )
                ).all()
            )
            project.status = (
                "clips_ready"
                if states == {"completed"}
                else (
                    "failed"
                    if "failed" in states
                    else "cancelled"
                    if "cancelled" in states
                    else "draft"
                )
            )
