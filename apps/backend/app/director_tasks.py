"""Charged director calls have durable intent and never receive automatic POST retries."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, or_, select

from app.generation import GenerationError, safe_code
from app.models import (
    ActualUsageCost,
    DirectorAttempt,
    DirectorJob,
    MediaAsset,
    Project,
    ProjectInput,
    Scene,
    Storyboard,
    User,
)
from app.security import aware_utc
from app.storyboard import director_context, director_gate, save_board, validate_payload
from app.storyboard_schemas import StoryboardPut


def locked_director(db, identifier):
    project_id = db.scalar(select(DirectorJob.project_id).where(DirectorJob.id == identifier))
    if project_id is None:
        return None
    db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    return db.scalar(select(DirectorJob).where(DirectorJob.id == identifier).with_for_update())


def dispatch_director(db, identifier, settings, dispatcher, *, countdown=0):
    row = locked_director(db, identifier)
    if row is None or row.status != "approved":
        return False
    director_gate(settings)
    db.commit()
    try:
        dispatcher("video.plan_storyboard", str(identifier), countdown=countdown)
        success = True
    except Exception:
        success = False
    row = locked_director(db, identifier)
    row.dispatch_state = "queued" if success else "error"
    row.dispatch_error_code = None if success else "broker_unavailable"
    # Repeated assignment of the same dispatch state may emit no UPDATE. Record
    # every broker attempt explicitly so bounded sweeps rotate fairly.
    row.updated_at = datetime.now(UTC)
    db.commit()
    return success


def _safe_usage(usage):
    if not isinstance(usage, dict):
        return {}
    return {
        key: value
        for key, value in usage.items()
        if key in {"input_tokens", "output_tokens", "total_tokens"}
        and type(value) is int
        and 0 <= value <= 10**9
    }


def _response_id(value):
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value) else None
    )


def _usage(db, job, usage, response_id):
    job.usage = _safe_usage(usage)
    job.response_id = _response_id(response_id)
    for record in db.scalars(
        select(ActualUsageCost).where(ActualUsageCost.director_job_id == job.id)
    ).all():
        record.usage_details = {
            "model": job.model,
            "token_usage": job.usage,
            "response_id": job.response_id,
            "reservation_held": True,
        }
        record.amount_usd = None
        record.status = "unavailable"


def _ambiguous_attempt(db, identifier):
    for attempt in db.scalars(
        select(DirectorAttempt).where(
            DirectorAttempt.job_id == identifier, DirectorAttempt.status == "started"
        )
    ).all():
        attempt.status = "ambiguous"
        attempt.error_code = "director_outcome_unknown"
        attempt.completed_at = datetime.now(UTC)


def _owned_attempt(db, job, attempt_id):
    # A duplicate delivery, recovery sweep or operator reconciliation can retire
    # this intent while the remote call is in flight. Only its still-started
    # attempt may publish a result/error or mutate billing evidence. Callers
    # already own the project and job locks, so this fence lasts through commit.
    if job is None or job.status != "submitting":
        return None
    attempt = db.scalar(
        select(DirectorAttempt)
        .where(DirectorAttempt.id == attempt_id, DirectorAttempt.job_id == job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return attempt if attempt and attempt.status == "started" else None


def _current_context(db, job, settings):
    project = db.get(Project, job.project_id)
    board = db.get(Storyboard, job.storyboard_id)
    source = db.get(ProjectInput, job.input_id)
    if (
        not project
        or not board
        or not source
        or board.revision != job.board_revision
        or board.input_id != source.id
        or settings.director_model != job.model
    ):
        return None
    if job.context_hash != director_context(
        db, project, board, source, settings.director_model, job.request_snapshot
    ):
        return None
    for reference in job.request_snapshot.get("reference_assets", []):
        asset = db.get(MediaAsset, UUID(reference["id"]))
        if (
            not asset
            or asset.project_id != job.project_id
            or asset.kind != "reference_image"
            or asset.status != "ready"
            or asset.sha256 != reference.get("sha256")
        ):
            return None
    return project, board, source


async def process_director(
    identifier,
    *,
    sessions,
    settings,
    director_factory,
    dispatcher,
    admission=None,
    provider_factory=None,
):
    from app.director import DirectorError, DirectorRequest
    from app.host_guard import require_admission
    from app.runtime import make_provider

    identifier = UUID(str(identifier))
    client = None
    validator = None
    try:
        with sessions() as db:
            job = locked_director(db, identifier)
            if not job or job.status not in {"approved", "submitting"}:
                return
            if job.status == "submitting":
                job.status = "reconciliation_required"
                job.error_code = "director_outcome_unknown"
                _ambiguous_attempt(db, identifier)
                db.commit()
                return
            try:
                director_gate(settings)
            except GenerationError as exc:
                job.error_code = exc.code
                job.dispatch_state = "error"
                db.commit()
                return
            context = _current_context(db, job, settings)
            approver = db.get(User, job.approved_by_id)
            if (
                not context
                or not approver
                or not approver.active
                or approver.role not in {"editor", "admin"}
            ):
                job.status = "stale"
                job.error_code = "director_context_changed"
                db.commit()
                return
            try:
                admitted = (admission or require_admission)(settings) is True
            except Exception:
                admitted = False
            if not admitted:
                job.error_code = "host_admission_refused"
                job.dispatch_state = "pending"
                db.commit()
                dispatch_director(
                    db,
                    identifier,
                    settings,
                    dispatcher,
                    countdown=settings.generation_poll_base_seconds,
                )
                return
            values = dict(job.request_snapshot)
            values["budget_usd"] = Decimal(values["budget_usd"])
            request = DirectorRequest(**values)
            client = director_factory(settings)
            attempt = DirectorAttempt(job_id=job.id, attempt_number=1, status="started")
            db.add(attempt)
            job.status = "submitting"
            job.submitted_at = datetime.now(UTC)
            job.error_code = None
            db.flush()
            attempt_id = attempt.id
            # The paid intent survives a lost/invalid response or worker restart.
            db.commit()
        try:
            result = await client.generate(request, approved=True)
        except Exception as exc:
            with sessions() as db:
                job = locked_director(db, identifier)
                attempt = _owned_attempt(db, job, attempt_id)
                if attempt is None:
                    return
                ambiguous = not isinstance(exc, DirectorError) or exc.ambiguous
                job.status = "reconciliation_required" if ambiguous else "failed"
                job.error_code = safe_code(getattr(exc, "code", None), "director_outcome_unknown")
                job.completed_at = datetime.now(UTC)
                job.dispatch_state = "not_needed"
                _usage(db, job, getattr(exc, "usage", {}), getattr(exc, "response_id", None))
                attempt.status = "ambiguous" if ambiguous else "failed"
                attempt.error_code = job.error_code
                attempt.completed_at = datetime.now(UTC)
                db.commit()
            return
        with sessions() as db:
            job = locked_director(db, identifier)
            attempt = _owned_attempt(db, job, attempt_id)
            if attempt is None:
                return
            _usage(db, job, result.usage, result.response_id)
            job.result_snapshot = result.plan.model_dump(mode="json")
            job.completed_at = datetime.now(UTC)
            job.dispatch_state = "not_needed"
            context = _current_context(db, job, settings)
            if not context:
                job.status = "stale"
                job.error_code = "director_context_changed"
                attempt.status = "stale"
                attempt.completed_at = datetime.now(UTC)
                db.commit()
                return
            project, board, _ = context
            existing = {
                scene.scene_key: scene.id
                for scene in db.scalars(select(Scene).where(Scene.project_id == project.id)).all()
            }
            allowed = {str(item["id"]) for item in job.request_snapshot["reference_assets"]}
            try:
                scenes = result.plan.model_dump(mode="json")["scenes"]
                if any(
                    str(reference) not in allowed
                    for scene in scenes
                    for reference in scene["reference_asset_ids"]
                ):
                    raise GenerationError("director_reference_outside_snapshot", 422)
                for scene in scenes:
                    scene["id"] = existing.get(scene["scene_id"])
                payload = StoryboardPut(
                    expected_revision=board.revision,
                    title=result.plan.title,
                    **{
                        key: job.request_snapshot[key]
                        for key in (
                            "target_duration_seconds",
                            "language",
                            "visual_style",
                            "target_audience",
                            "default_model_id",
                        )
                    },
                    scenes=scenes,
                )
                validator = (provider_factory or make_provider)(settings)
                validation = await validate_payload(db, project, payload, settings, validator)
                if not validation["valid"]:
                    raise GenerationError("director_plan_validation_failed", 422)
                save_board(
                    db,
                    project.id,
                    db.get(User, job.approved_by_id),
                    payload,
                    commit=False,
                    ignore_director_id=job.id,
                )
                job.status = "succeeded"
                job.result_revision = board.revision
                job.error_code = None
                attempt.status = "succeeded"
            except (GenerationError, ValueError) as exc:
                job.status = "failed"
                job.error_code = safe_code(
                    getattr(exc, "code", None), "director_plan_validation_failed"
                )
                attempt.status = "failed"
                attempt.error_code = job.error_code
            attempt.completed_at = datetime.now(UTC)
            db.commit()
    finally:
        for provider in (client, validator):
            if provider is not None:
                try:
                    await provider.aclose()
                except Exception:
                    pass


def _runtime(settings=None, sessions=None, director_factory=None, dispatcher=None):
    from app.config import get_settings
    from app.db import make_engine, make_session_factory

    settings = settings or get_settings()
    engine = None
    if sessions is None:
        engine = make_engine(settings.database_url)
        sessions = make_session_factory(engine)
    if dispatcher is None:
        from app.runtime import dispatch_director as runtime_dispatch

        dispatcher = runtime_dispatch
    if director_factory is None:
        from app.runtime import make_director

        director_factory = make_director
    return settings, sessions, director_factory, dispatcher, engine


def plan_storyboard(
    identifier,
    *,
    sessions=None,
    settings=None,
    director_factory=None,
    dispatcher=None,
    admission=None,
    provider_factory=None,
):
    settings, sessions, factory, dispatcher, engine = _runtime(
        settings, sessions, director_factory, dispatcher
    )
    try:
        return asyncio.run(
            process_director(
                identifier,
                sessions=sessions,
                settings=settings,
                director_factory=factory,
                dispatcher=dispatcher,
                admission=admission,
                provider_factory=provider_factory,
            )
        )
    finally:
        if engine is not None:
            engine.dispose()


def reconcile_directors(*, sessions=None, settings=None, director_factory=None, dispatcher=None):
    settings, sessions, _, dispatcher, engine = _runtime(
        settings, sessions, director_factory, dispatcher
    )
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.director_stale_seconds)
        # Paid intents always receive an independent sweep, even when the
        # feature is disabled or the pre-submission dispatch backlog is large.
        with sessions() as db:
            identifiers = db.scalars(
                select(DirectorJob.id)
                .where(
                    DirectorJob.status == "submitting",
                    or_(DirectorJob.submitted_at.is_(None), DirectorJob.submitted_at <= cutoff),
                )
                .order_by(DirectorJob.updated_at)
                .limit(100)
            ).all()
        for identifier in identifiers:
            with sessions() as db:
                job = locked_director(db, identifier)
                if job and job.status == "submitting":
                    if job.submitted_at and aware_utc(job.submitted_at) > cutoff:
                        continue
                    job.status = "reconciliation_required"
                    job.error_code = "director_outcome_unknown"
                    _ambiguous_attempt(db, identifier)
                    db.commit()
        if not settings.director_enabled:
            return
        with sessions() as db:
            identifiers = db.scalars(
                select(DirectorJob.id)
                .where(
                    DirectorJob.status == "approved",
                    or_(
                        DirectorJob.dispatch_state.in_(["pending", "error"]),
                        and_(
                            DirectorJob.dispatch_state == "queued", DirectorJob.updated_at <= cutoff
                        ),
                    ),
                )
                .order_by(DirectorJob.updated_at, DirectorJob.id)
                .limit(100)
            ).all()
        for identifier in identifiers:
            with sessions() as db:
                job = locked_director(db, identifier)
                if not job or job.status != "approved":
                    continue
                if job.dispatch_state == "queued" and aware_utc(job.updated_at) > cutoff:
                    continue
                if job.dispatch_state in {"pending", "error", "queued"}:
                    dispatch_director(db, identifier, settings, dispatcher)
    finally:
        if engine is not None:
            engine.dispose()
