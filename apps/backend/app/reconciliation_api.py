"""Explicit verified outcomes/bills; no action submits or releases reservations."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies import administrator, get_db
from app.generation import (
    GenerationError,
    dispatch_job,
    fingerprint,
    json_value,
    owned_project,
    restore_model,
    update_scene_state,
)
from app.library_schemas import MetadataIn, MutationIn
from app.models import (
    ActualUsageCost,
    AuditEvent,
    DirectorJob,
    GenerationJob,
    User,
    WorkspaceMutation,
)
from app.providers.base import ProviderError
from app.security import aware_utc

router = APIRouter(prefix="/api/v1/operations", tags=["reconciliation"])


class VerifiedIn(MutationIn):
    expected_updated_at: datetime
    reference: str = Field(min_length=3, max_length=240)
    acknowledge_verified: Literal[True]
    _aware = field_validator("expected_updated_at")(MetadataIn.aware_timestamp.__func__)

    @field_validator("reference")
    @classmethod
    def safe_reference(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("A verified provider statement or support reference is required")
        return value.strip()


class ReconcileIn(VerifiedIn):
    outcome: Literal["verified_failed", "attach_provider_id"]
    provider_job_id: UUID | None = None


class ConfirmCostIn(VerifiedIn):
    amount_usd: Decimal = Field(ge=0, le=1000000, decimal_places=6)
    provider_charge_id: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$"
    )


def mutation(db, user, operation, identifier, payload):
    # Same actor -> project -> entity lock order as library actions. Hashes bind
    # mutation keys to their target, operator evidence and complete request.
    actor = db.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if actor is None or not actor.active or actor.role != "admin":
        raise HTTPException(403, "Administrator role required")
    digest = fingerprint({"id": identifier, **payload.model_dump(mode="json")})
    previous = db.scalar(
        select(WorkspaceMutation).where(
            WorkspaceMutation.actor_user_id == user.id,
            WorkspaceMutation.operation == operation,
            WorkspaceMutation.idempotency_key == payload.idempotency_key,
        )
    )
    if previous and previous.request_fingerprint != digest:
        raise HTTPException(409, "idempotency_conflict")
    return previous, digest


def event(db, user, project, entity, kind, payload):
    entity_type = (
        "usage_cost"
        if isinstance(entity, ActualUsageCost)
        else "generation_job"
        if isinstance(entity, GenerationJob)
        else "director_job"
    )
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            project_id=project.id,
            entity_type=entity_type,
            entity_id=entity.id,
            event_type=kind,
            details={"reference": payload.reference, "actor_verified": True},
        )
    )


def job_dto(job, kind):
    return json_value(
        {
            "id": job.id,
            "kind": kind,
            "project_id": job.project_id,
            "provider": job.provider if kind == "generation" else "openai",
            "model_id": job.model_id if kind == "generation" else job.model,
            "status": job.status,
            "updated_at": job.updated_at,
            "reserved_usd": job.reserved_cost_usd,
            "provider_job_id": job.provider_job_id if kind == "generation" else job.response_id,
        }
    )


def cost_dto(row):
    return json_value(
        {
            key: getattr(row, key)
            for key in (
                "id",
                "project_id",
                "provider",
                "model_id",
                "generation_job_id",
                "director_job_id",
                "rendering_job_id",
                "status",
                "amount_usd",
                "provider_charge_id",
                "updated_at",
            )
        }
    )


@router.get("/reconciliations")
def unresolved(
    kind: Literal["generation", "director"] = "generation",
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    _user=Depends(administrator),
    db: Session = Depends(get_db),
):
    model = GenerationJob if kind == "generation" else DirectorJob
    query = select(model).where(model.status == "reconciliation_required")
    return {
        "items": [
            job_dto(row, kind)
            for row in db.scalars(
                query.order_by(model.created_at, model.id).limit(limit).offset(offset)
            )
        ],
        "total": db.scalar(select(func.count()).select_from(query.subquery())),
        "limit": limit,
        "offset": offset,
    }


@router.post("/reconciliations/{kind}/{job_id}")
async def reconcile(
    kind: Literal["generation", "director"],
    job_id: UUID,
    payload: ReconcileIn,
    request: Request,
    user=Depends(administrator),
    db: Session = Depends(get_db),
):
    operation = f"reconcile_{kind}"
    previous, digest = mutation(db, user, operation, job_id, payload)
    model = GenerationJob if kind == "generation" else DirectorJob
    job = db.get(model, job_id)
    if job is None:
        raise HTTPException(404, "job_not_found")
    project = owned_project(db, job.project_id, user, lock=True)
    job = db.scalar(
        select(model)
        .where(model.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if previous:
        return job_dto(job, kind)
    if aware_utc(job.updated_at) != payload.expected_updated_at:
        raise HTTPException(409, "revision_conflict")
    if job.status != "reconciliation_required":
        raise HTTPException(409, "reconciliation_not_required")
    now = datetime.now(UTC)
    if payload.outcome == "attach_provider_id":
        if kind != "generation" or payload.provider_job_id is None:
            raise HTTPException(422, "verified_generation_request_id_required")
        provider_id = str(payload.provider_job_id)
        if job.provider_job_id and job.provider_job_id != provider_id:
            raise HTTPException(409, "provider_request_mismatch")
        if db.scalar(
            select(GenerationJob.id).where(
                GenerationJob.provider == job.provider,
                GenerationJob.provider_job_id == provider_id,
                GenerationJob.id != job.id,
            )
        ):
            raise HTTPException(409, "provider_request_already_attached")
        provider = request.app.state.provider_factory(request.app.state.settings)
        try:
            submission = provider.reconcile_submission(
                restore_model(job.model_snapshot), provider_id
            )
        except (ProviderError, ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(422, "provider_reconciliation_invalid") from None
        finally:
            if hasattr(provider, "aclose"):
                await provider.aclose()
        job.provider_job_id = submission.request_id
        job.submission_snapshot = submission.model_dump(mode="json")
        job.status = "cancellation_requested" if job.no_longer_wanted else "submitted"
        job.next_poll_at = now
        job.operation_token = job.operation_started_at = None
        job.poll_count, job.last_error_code = 0, None
        job.dispatch_state = "pending"
    else:
        if payload.provider_job_id is not None:
            raise HTTPException(422, "unexpected_provider_request_id")
        job.status, job.completed_at, job.dispatch_state = "failed", now, "not_needed"
        if kind == "generation":
            job.last_error_code = "operator_verified_failure"
            job.operation_token = job.operation_started_at = None
            update_scene_state(db, job, "failed")
        else:
            job.error_code = "operator_verified_failure"
            if project.status == "planning":
                project.status = "failed"
    event(db, user, project, job, "operator." + payload.outcome, payload)
    db.add(
        WorkspaceMutation(
            actor_user_id=user.id,
            operation=operation,
            idempotency_key=payload.idempotency_key,
            request_fingerprint=digest,
            result_project_id=project.id,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "reconciliation_conflict") from None
    # Existing IDs dispatch poll/cancellation only. Never a paid submission.
    if payload.outcome == "attach_provider_id":
        try:
            dispatch_job(
                db, job.id, request.app.state.settings, request.app.state.generation_dispatcher
            )
        except GenerationError:
            pass
    return job_dto(job, kind)


@router.get("/costs")
def unconfirmed_costs(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    _user=Depends(administrator),
    db: Session = Depends(get_db),
):
    query = select(ActualUsageCost).where(
        or_(ActualUsageCost.status != "confirmed", ActualUsageCost.amount_usd.is_(None))
    )
    return {
        "items": [
            cost_dto(row)
            for row in db.scalars(
                query.order_by(ActualUsageCost.created_at, ActualUsageCost.id)
                .limit(limit)
                .offset(offset)
            )
        ],
        "total": db.scalar(select(func.count()).select_from(query.subquery())),
        "limit": limit,
        "offset": offset,
    }


@router.post("/costs/{cost_id}/confirm")
def confirm_cost(
    cost_id: UUID,
    payload: ConfirmCostIn,
    user=Depends(administrator),
    db: Session = Depends(get_db),
):
    previous, digest = mutation(db, user, "confirm_cost", cost_id, payload)
    row = db.get(ActualUsageCost, cost_id)
    if row is None:
        raise HTTPException(404, "cost_not_found")
    project = owned_project(db, row.project_id, user, lock=True)
    row = db.scalar(
        select(ActualUsageCost)
        .where(ActualUsageCost.id == cost_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if previous:
        return cost_dto(row)
    if aware_utc(row.updated_at) != payload.expected_updated_at:
        raise HTTPException(409, "revision_conflict")
    if row.status == "confirmed" and row.amount_usd is not None:
        raise HTTPException(409, "charge_already_confirmed")
    for model, identifier, terminal in (
        (GenerationJob, row.generation_job_id, {"succeeded", "failed", "cancelled", "abandoned"}),
        (DirectorJob, row.director_job_id, {"succeeded", "failed", "stale"}),
    ):
        if identifier:
            job = db.get(model, identifier)
            if job is None or job.status not in terminal:
                raise HTTPException(409, "reconcile_job_before_confirming_charge")
    row.status, row.amount_usd = "confirmed", payload.amount_usd
    row.provider_charge_id = payload.provider_charge_id
    row.usage_details = {
        **(row.usage_details or {}),
        "billing": "operator_verified",
        "verified_by": str(user.id),
        "verified_at": datetime.now(UTC).isoformat(),
        "reference": payload.reference,
    }
    event(db, user, project, row, "operator.charge_confirmed", payload)
    db.add(
        WorkspaceMutation(
            actor_user_id=user.id,
            operation="confirm_cost",
            idempotency_key=payload.idempotency_key,
            request_fingerprint=digest,
            result_project_id=project.id,
            result_usage_cost_id=row.id,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "charge_confirmation_conflict") from None
    return cost_dto(row)
