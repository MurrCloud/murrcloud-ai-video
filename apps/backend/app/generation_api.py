"""Authenticated estimate, approval and recovery controls; no paid calls in HTTP handlers."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import current_user, editor, get_db
from app.generation import (
    GenerationError,
    approve_estimate,
    create_estimate,
    dispatch_job,
    job_response,
    owned_project,
    project_job,
    request_cancel,
)
from app.generation_schemas import (
    GenerationApprovalIn,
    GenerationEstimateIn,
    GenerationEstimateOut,
    GenerationJobOut,
)
from app.models import GenerationJob, User

router = APIRouter(prefix="/api/v1", tags=["generation"])


@router.post(
    "/projects/{project_id}/generations/estimate",
    response_model=GenerationEstimateOut,
    status_code=201,
)
async def estimate(
    project_id: UUID,
    payload: GenerationEstimateIn,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    provider = request.app.state.provider_factory(request.app.state.settings)
    try:
        row = await create_estimate(
            db, project_id, user, payload, request.app.state.settings, provider
        )
        return {
            "id": row.id,
            "base_usd": row.base_usd,
            "total_usd": row.total_usd,
            "pricing_verified": row.pricing_verified,
            "source": row.pricing_source,
            "expires_at": row.expires_at,
            "request": row.request_snapshot,
            "scene_id": row.scene_id,
            "retry_allowance_percent": row.retry_allowance_percent,
        }
    except GenerationError as exc:
        raise HTTPException(exc.status, exc.code) from None
    finally:
        await provider.aclose()


@router.post("/projects/{project_id}/generations", response_model=GenerationJobOut, status_code=202)
def approve(
    project_id: UUID,
    payload: GenerationApprovalIn,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        job = approve_estimate(db, project_id, user, payload, request.app.state.settings)
        dispatch_job(
            db, job.id, request.app.state.settings, request.app.state.generation_dispatcher
        )
        db.refresh(job)
        return job_response(db, job)
    except GenerationError as exc:
        raise HTTPException(exc.status, exc.code) from None


@router.get("/projects/{project_id}/generations", response_model=list[GenerationJobOut])
def jobs(
    project_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
):
    try:
        owned_project(db, project_id, user)
        rows = db.scalars(
            select(GenerationJob)
            .where(GenerationJob.project_id == project_id)
            .order_by(GenerationJob.created_at.desc(), GenerationJob.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [job_response(db, job) for job in rows]
    except GenerationError as exc:
        raise HTTPException(exc.status, exc.code) from None


@router.get("/projects/{project_id}/generations/{job_id}", response_model=GenerationJobOut)
def detail(
    project_id: UUID,
    job_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        return job_response(db, project_job(db, project_id, job_id, user))
    except GenerationError as exc:
        raise HTTPException(exc.status, exc.code) from None


@router.post(
    "/projects/{project_id}/generations/{job_id}/dispatch",
    response_model=GenerationJobOut,
    status_code=202,
)
def dispatch(
    project_id: UUID,
    job_id: UUID,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        job = project_job(db, project_id, job_id, user)
        dispatch_job(
            db, job.id, request.app.state.settings, request.app.state.generation_dispatcher
        )
        db.refresh(job)
        return job_response(db, job)
    except GenerationError as exc:
        raise HTTPException(exc.status, exc.code) from None


@router.post(
    "/projects/{project_id}/generations/{job_id}/cancel",
    response_model=GenerationJobOut,
    status_code=202,
)
def cancel(
    project_id: UUID,
    job_id: UUID,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        job = request_cancel(db, project_id, job_id, user)
        dispatch_job(
            db, job.id, request.app.state.settings, request.app.state.generation_dispatcher
        )
        db.refresh(job)
        return job_response(db, job)
    except GenerationError as exc:
        raise HTTPException(exc.status, exc.code) from None
