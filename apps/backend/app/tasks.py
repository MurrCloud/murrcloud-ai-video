"""Restart-safe worker entry points. Each paid POST has exactly one durable intent.

Dependencies are injectable for offline tests. Production wrappers supply none.
No Celery retry decorator is used: only GET polling/download receive bounded
backoff. A crash after paid intent always needs reconciliation, never resubmit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.generation import (
    TERMINAL,
    GenerationError,
    current_model,
    dispatch_job,
    paid_gate,
    restore_model,
    safe_code,
    snapshot_model,
    update_scene_state,
)
from app.models import CostEstimate, GenerationAttempt, GenerationJob, MediaAsset, Project, User
from app.providers.base import ProviderError, Submission, VideoRequest
from app.security import aware_utc


def _admitted(settings, admission):
    try:
        if admission is None:
            from app.host_guard import require_admission

            admission = require_admission
        return admission(settings) is True
    except Exception:
        return False


def _runtime(settings, sessions, provider_factory, dispatcher):
    from app.config import get_settings
    from app.db import make_engine, make_session_factory
    from app.runtime import dispatch_generation, make_provider

    settings = settings or get_settings()
    engine = None
    if sessions is None:
        engine = make_engine(settings.database_url)
        sessions = make_session_factory(engine)
    return (
        settings,
        sessions,
        provider_factory or make_provider,
        dispatcher or dispatch_generation,
        engine,
    )


def _locked(db, job_id):
    # Global mutation order: Project -> GenerationJob -> Scene/Version.
    # Reading the immutable foreign key takes no row lock while approval or
    # cancellation finishes at the project serialization boundary.
    project_id = db.scalar(select(GenerationJob.project_id).where(GenerationJob.id == job_id))
    if project_id is None:
        return None
    db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    return db.scalar(select(GenerationJob).where(GenerationJob.id == job_id).with_for_update())


def _dispatch(sessions, job_id, settings, dispatcher, *, countdown=0):
    with sessions() as db:
        return dispatch_job(db, job_id, settings, dispatcher, countdown=countdown)


def _clear_operation(job):
    job.operation_token = None
    job.operation_started_at = None


async def _close(provider):
    if provider is not None:
        try:
            await provider.aclose()
        except Exception:
            pass


async def process_submission(
    job_id, *, sessions, settings, provider_factory, dispatcher, admission=None
):
    job_id = UUID(str(job_id))
    provider = None
    attempt_id = None
    try:
        with sessions() as db:
            job = _locked(db, job_id)
            if not job or job.status in TERMINAL:
                return
            if job.provider_job_id:
                db.commit()
                _dispatch(sessions, job_id, settings, dispatcher)
                return
            if job.status in {"submitting", "cancellation_requested"}:
                job.status = "reconciliation_required"
                job.last_error_code = "submission_outcome_unknown"
                db.commit()
                return
            if job.status != "approved":
                return
            if job.no_longer_wanted:
                job.status = "cancelled"
                job.completed_at = datetime.now(UTC)
                update_scene_state(db, job, "cancelled")
                db.commit()
                return
            try:
                paid_gate(settings)
            except GenerationError as exc:
                job.last_error_code = exc.code
                job.dispatch_state = "error"
                job.dispatch_error_code = exc.code
                db.commit()
                return
            try:
                estimate = db.get(CostEstimate, job.estimate_id) if job.estimate_id else None
                approver = db.get(User, job.approved_by_id) if job.approved_by_id else None
                if (
                    not estimate
                    or estimate.status != "approved"
                    or not approver
                    or not approver.active
                    or approver.role not in {"admin", "editor"}
                    or not job.approved_at
                    or job.reserved_cost_usd <= 0
                ):
                    raise GenerationError("approved_intent_missing")
                if (
                    job.request_snapshot != estimate.request_snapshot
                    or job.model_snapshot != estimate.model_snapshot
                ):
                    raise GenerationError("approved_inputs_changed")
                if snapshot_model(current_model(db, job.model_id)) != job.model_snapshot:
                    raise GenerationError("model_changed_since_approval")
                model = restore_model(job.model_snapshot)
                request = VideoRequest.model_validate(job.request_snapshot)
                provider = provider_factory(settings)
                await provider.validate_request(model, request)
            except Exception as exc:
                job.status = "failed"
                job.last_error_code = safe_code(
                    getattr(exc, "code", None), "approved_input_validation_failed"
                )
                job.completed_at = datetime.now(UTC)
                update_scene_state(db, job, "failed")
                db.commit()
                return
            if not _admitted(settings, admission):
                job.last_error_code = "host_admission_refused"
                job.next_poll_at = datetime.now(UTC) + timedelta(
                    seconds=settings.generation_poll_base_seconds
                )
                job.dispatch_state = "pending"
                db.commit()
                _dispatch(
                    sessions,
                    job_id,
                    settings,
                    dispatcher,
                    countdown=settings.generation_poll_base_seconds,
                )
                return
            number = (
                db.scalar(
                    select(func.max(GenerationAttempt.attempt_number)).where(
                        GenerationAttempt.job_id == job_id
                    )
                )
                or 0
            ) + 1
            attempt = GenerationAttempt(
                job_id=job_id, attempt_number=number, operation="submit", status="started"
            )
            db.add(attempt)
            job.status = "submitting"
            job.operation_token = uuid4()
            job.operation_started_at = datetime.now(UTC)
            job.last_error_code = None
            update_scene_state(db, job, "generating")
            db.flush()
            attempt_id = attempt.id
            # This transaction is durable BEFORE contacting a paid endpoint.
            db.commit()
        try:
            submitted = await provider.submit_generation(model, request, paid_authorized=True)
            submission = Submission.model_validate(submitted.model_dump(mode="json"))
        except Exception as exc:
            ambiguous = not isinstance(exc, ProviderError) or exc.ambiguous
            with sessions() as db:
                job = _locked(db, job_id)
                attempt = db.get(GenerationAttempt, attempt_id)
                job.status = "reconciliation_required" if ambiguous else "failed"
                job.last_error_code = safe_code(
                    getattr(exc, "code", None), "submission_outcome_unknown"
                )
                if not ambiguous:
                    job.completed_at = datetime.now(UTC)
                    update_scene_state(db, job, "failed")
                _clear_operation(job)
                attempt.status = "ambiguous" if ambiguous else "failed"
                attempt.error_code = job.last_error_code
                attempt.completed_at = datetime.now(UTC)
                db.commit()
            return
        try:
            with sessions() as db:
                job = _locked(db, job_id)
                attempt = db.get(GenerationAttempt, attempt_id)
                if job.provider_job_id and job.provider_job_id != submission.request_id:
                    raise IntegrityError("provider_identity_conflict", None, None)
                job.provider_job_id = submission.request_id
                job.submission_snapshot = submission.model_dump(mode="json")
                job.submitted_at = datetime.now(UTC)
                job.status = "cancellation_requested" if job.no_longer_wanted else "submitted"
                job.next_poll_at = datetime.now(UTC) + timedelta(
                    seconds=settings.generation_poll_base_seconds
                )
                job.dispatch_state = "pending"
                job.last_error_code = None
                _clear_operation(job)
                attempt.status = "succeeded"
                attempt.provider_request_id = submission.request_id
                attempt.completed_at = datetime.now(UTC)
                db.commit()
        except IntegrityError:
            with sessions() as db:
                job = _locked(db, job_id)
                job.status = "reconciliation_required"
                job.last_error_code = "provider_identity_conflict"
                _clear_operation(job)
                attempt = db.get(GenerationAttempt, attempt_id)
                attempt.status = "ambiguous"
                attempt.provider_request_id = submission.request_id
                attempt.error_code = "provider_identity_conflict"
                attempt.completed_at = datetime.now(UTC)
                db.commit()
            return
        _dispatch(
            sessions, job_id, settings, dispatcher, countdown=settings.generation_poll_base_seconds
        )
    finally:
        await _close(provider)


def _claim_poll(sessions, job_id, settings):
    with sessions() as db:
        job = _locked(db, job_id)
        if not job or job.status in TERMINAL or not job.provider_job_id:
            return None
        now = datetime.now(UTC)
        if job.next_poll_at and aware_utc(job.next_poll_at) > now + timedelta(seconds=1):
            return None
        lease_seconds = settings.download_timeout_seconds + 60
        if (
            job.operation_token
            and job.operation_started_at
            and aware_utc(job.operation_started_at) > now - timedelta(seconds=lease_seconds)
        ):
            return None
        if job.poll_count >= settings.generation_poll_max_attempts:
            job.status = "reconciliation_required"
            job.last_error_code = "poll_limit_reached"
            job.next_poll_at = None
            _clear_operation(job)
            db.commit()
            return None
        job.poll_count += 1
        job.operation_token = uuid4()
        job.operation_started_at = now
        token = job.operation_token
        db.commit()
        return job, token


def _poll_outcome(
    sessions, job_id, token, settings, *, state=None, error=None, retry=True, asset=None
):
    with sessions() as db:
        job = _locked(db, job_id)
        if not job or job.operation_token != token:
            return None
        _clear_operation(job)
        job.last_error_code = error
        if asset is not None:
            unwanted = job.no_longer_wanted
            media = MediaAsset(
                project_id=job.project_id,
                scene_version_id=job.scene_version_id,
                kind="clip",
                object_key=asset.storage_key,
                mime_type=asset.mime_type,
                size_bytes=asset.size_bytes,
                sha256=asset.sha256,
                storage_backend="local",
                status="rejected" if unwanted else "ready",
                metadata_json={"generation_job_id": str(job_id), "no_longer_wanted": unwanted},
            )
            db.add(media)
            db.flush()
            job.output_asset_id = None if unwanted else media.id
            state = "abandoned" if unwanted else "succeeded"
            if unwanted:
                job.last_error_code = "completed_result_not_wanted"
        if state in TERMINAL:
            job.status = state
            job.completed_at = datetime.now(UTC)
            job.next_poll_at = None
            job.dispatch_state = "not_needed"
            update_scene_state(
                db,
                job,
                "completed"
                if state == "succeeded"
                else "cancelled"
                if state == "abandoned"
                else state,
            )
            db.commit()
            return None
        if not retry or job.poll_count >= settings.generation_poll_max_attempts:
            job.status = "reconciliation_required"
            job.next_poll_at = None
            job.dispatch_state = "not_needed"
            if retry:
                job.last_error_code = "poll_limit_reached"
            db.commit()
            return None
        job.status = (
            "cancellation_requested"
            if job.no_longer_wanted
            else state
            if state in {"queued", "running"}
            else "submitted"
        )
        delay = min(
            settings.generation_poll_max_seconds,
            settings.generation_poll_base_seconds * 2 ** min(max(job.poll_count - 1, 0), 16),
        )
        job.next_poll_at = datetime.now(UTC) + timedelta(seconds=delay)
        job.dispatch_state = "pending"
        db.commit()
        return delay


async def process_poll(
    job_id, *, sessions, settings, provider_factory, dispatcher, downloader, admission=None
):
    job_id = UUID(str(job_id))
    claimed = _claim_poll(sessions, job_id, settings)
    if claimed is None:
        return
    job, token = claimed
    provider = None
    delay = None
    try:
        provider = provider_factory(settings)
        model = restore_model(job.model_snapshot)
        submission = Submission.model_validate(job.submission_snapshot)
        status = await provider.get_generation_status(model, submission)
        if status.state == "completed":
            with sessions() as db:
                current = _locked(db, job_id)
                unwanted = current.no_longer_wanted
                db.commit()
            if unwanted:
                delay = _poll_outcome(
                    sessions,
                    job_id,
                    token,
                    settings,
                    state="abandoned",
                    error="completed_result_not_wanted",
                    retry=False,
                )
            elif not _admitted(settings, admission):
                delay = _poll_outcome(
                    sessions, job_id, token, settings, error="host_admission_refused"
                )
            else:
                result = await provider.retrieve_result(model, submission)
                stored = await downloader(
                    result.video_url, project_id=job.project_id, job_id=job_id, settings=settings
                )
                delay = _poll_outcome(sessions, job_id, token, settings, asset=stored)
        elif status.state in {"failed", "cancelled"}:
            delay = _poll_outcome(
                sessions,
                job_id,
                token,
                settings,
                state=status.state,
                error=safe_code(status.error_code) if status.error_code else None,
                retry=False,
            )
        elif status.state in {"queued", "running"}:
            delay = _poll_outcome(sessions, job_id, token, settings, state=status.state)
        else:
            delay = _poll_outcome(
                sessions, job_id, token, settings, error="invalid_provider_state", retry=False
            )
    except Exception as exc:
        delay = _poll_outcome(
            sessions,
            job_id,
            token,
            settings,
            error=safe_code(getattr(exc, "code", None), "poll_or_download_failed"),
            retry=getattr(exc, "retryable", True),
        )
    finally:
        await _close(provider)
    if delay is not None:
        # GET polling is the sole automatic retry path after a paid submission.
        _dispatch(sessions, job_id, settings, dispatcher, countdown=delay)


async def process_cancel(job_id, *, sessions, settings, provider_factory, dispatcher):
    job_id = UUID(str(job_id))
    provider = None
    with sessions() as db:
        job = _locked(db, job_id)
        if not job or not job.no_longer_wanted or job.status in TERMINAL or not job.provider_job_id:
            return
        now = datetime.now(UTC)
        if (
            job.operation_token
            and job.operation_started_at
            and aware_utc(job.operation_started_at)
            > now - timedelta(seconds=settings.download_timeout_seconds + 60)
        ):
            # A poll/download owns the lease. It observes no_longer_wanted.
            return
        token = uuid4()
        job.operation_token = token
        job.operation_started_at = now
        job.cancel_attempted_at = now
        db.commit()
    try:
        provider = provider_factory(settings)
        outcome = await provider.cancel_generation(
            restore_model(job.model_snapshot), Submission.model_validate(job.submission_snapshot)
        )
        with sessions() as db:
            current = _locked(db, job_id)
            if current.operation_token != token:
                return
            _clear_operation(current)
            if outcome.confirmed_stopped:
                current.status = "cancelled"
                current.completed_at = datetime.now(UTC)
                current.next_poll_at = None
                current.dispatch_state = "not_needed"
                update_scene_state(db, current, "cancelled")
            else:
                current.status = "cancellation_requested"
                current.last_error_code = "cancellation_not_confirmed"
                current.next_poll_at = datetime.now(UTC) + timedelta(
                    seconds=settings.generation_poll_base_seconds
                )
            db.commit()
    except Exception as exc:
        with sessions() as db:
            current = _locked(db, job_id)
            if current.operation_token == token:
                _clear_operation(current)
                current.status = "cancellation_requested"
                current.last_error_code = safe_code(
                    getattr(exc, "code", None), "cancellation_not_confirmed"
                )
                current.next_poll_at = datetime.now(UTC) + timedelta(
                    seconds=settings.generation_poll_base_seconds
                )
                db.commit()
    finally:
        await _close(provider)
    # Never automatically repeat the cancellation request; poll the known ID.
    with sessions() as db:
        current = _locked(db, job_id)
        if current.status not in TERMINAL:
            db.commit()
            try:
                dispatcher(
                    "video.poll_generation",
                    str(job_id),
                    countdown=settings.generation_poll_base_seconds,
                )
            except Exception:
                current = _locked(db, job_id)
                current.dispatch_state = "error"
                current.dispatch_error_code = "broker_unavailable"
                db.commit()


def _run(
    operation,
    job_id,
    *,
    sessions=None,
    settings=None,
    provider_factory=None,
    dispatcher=None,
    downloader=None,
    admission=None,
):
    settings, sessions, provider_factory, dispatcher, engine = _runtime(
        settings, sessions, provider_factory, dispatcher
    )
    arguments = dict(
        sessions=sessions,
        settings=settings,
        provider_factory=provider_factory,
        dispatcher=dispatcher,
    )
    if operation is process_poll:
        if downloader is None:
            from app.storage import download_provider_video

            downloader = download_provider_video
        arguments.update(downloader=downloader, admission=admission)
    elif operation is process_submission:
        arguments["admission"] = admission
    try:
        return asyncio.run(operation(job_id, **arguments))
    finally:
        if engine is not None:
            engine.dispose()


def submit_generation(job_id, **kwargs):
    return _run(process_submission, job_id, **kwargs)


def poll_generation(job_id, **kwargs):
    return _run(process_poll, job_id, **kwargs)


def cancel_generation(job_id, **kwargs):
    return _run(process_cancel, job_id, **kwargs)


def reconcile_pending(*, sessions=None, settings=None, dispatcher=None, **_):
    settings, sessions, _, dispatcher, engine = _runtime(settings, sessions, None, dispatcher)
    try:
        now = datetime.now(UTC)
        lease_cutoff = now - timedelta(seconds=settings.download_timeout_seconds + 60)
        eligible_states = ["submitting", "submitted", "queued", "running", "cancellation_requested"]
        if settings.paid_generation_enabled:
            eligible_states.append("approved")
        with sessions() as db:
            jobs = db.scalars(
                select(GenerationJob)
                .where(
                    GenerationJob.status.in_(eligible_states),
                    or_(GenerationJob.next_poll_at.is_(None), GenerationJob.next_poll_at <= now),
                    or_(
                        GenerationJob.operation_token.is_(None),
                        GenerationJob.operation_started_at.is_(None),
                        GenerationJob.operation_started_at <= lease_cutoff,
                    ),
                    or_(
                        GenerationJob.provider_job_id.is_not(None),
                        GenerationJob.status.in_(["approved", "submitting"]),
                    ),
                )
                .order_by(GenerationJob.updated_at)
                .limit(100)
            ).all()
            identifiers = [job.id for job in jobs]
        for job_id in identifiers:
            with sessions() as db:
                job = _locked(db, job_id)
                if not job:
                    continue
                if not job.provider_job_id and job.status == "submitting":
                    if job.operation_started_at and aware_utc(
                        job.operation_started_at
                    ) > now - timedelta(seconds=settings.download_timeout_seconds + 60):
                        continue
                    job.status = "reconciliation_required"
                    job.last_error_code = "submission_outcome_unknown"
                    _clear_operation(job)
                    db.commit()
                    continue
                if job.next_poll_at and aware_utc(job.next_poll_at) > now:
                    continue
                if job.status == "reconciliation_required":
                    continue
                if job.operation_token:
                    if job.operation_started_at and aware_utc(
                        job.operation_started_at
                    ) > now - timedelta(seconds=settings.download_timeout_seconds + 60):
                        continue
                    if not job.provider_job_id:
                        job.status = "reconciliation_required"
                        job.last_error_code = "submission_outcome_unknown"
                        _clear_operation(job)
                        db.commit()
                        continue
                    # The old worker lease expired. Known-ID GETs may be retried;
                    # no path here creates another paid submission intent.
                    _clear_operation(job)
                db.commit()
                try:
                    dispatch_job(db, job_id, settings, dispatcher)
                except GenerationError:
                    db.rollback()
    finally:
        if engine is not None:
            engine.dispose()
