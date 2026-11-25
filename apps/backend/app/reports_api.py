from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dependencies import current_user, get_db
from app.generation import GenerationError
from app.models import DirectorJob, GenerationJob, Project
from app.reports import project_scope, usage_report

router = APIRouter(prefix="/api/v1", tags=["reports"])


@router.get("/reports/usage")
def usage(
    group_by: Literal["project", "scene", "model", "provider"] = "project",
    project_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        return usage_report(
            db, user, group_by=group_by, project_id=project_id, limit=limit, offset=offset
        )
    except GenerationError as exc:
        raise HTTPException(exc.status, exc.code) from None


@router.get("/dashboard")
def dashboard(user=Depends(current_user), db: Session = Depends(get_db)):
    from app.library import list_projects

    scope = project_scope(user)
    counts = dict(
        db.execute(
            select(Project.status, func.count())
            .where(Project.id.in_(scope), Project.archived.is_(False))
            .group_by(Project.status)
        ).all()
    )
    costs = usage_report(db, user, limit=1)["totals"]
    unresolved = sum(
        db.scalar(
            select(func.count())
            .select_from(model)
            .where(model.project_id.in_(scope), model.status == "reconciliation_required")
        )
        for model in (GenerationJob, DirectorJob)
    )
    return {
        "projects_total": sum(counts.values()),
        "in_production": sum(counts.get(s, 0) for s in ("generating", "narrating", "rendering")),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "recent_projects": list_projects(db, user, limit=8)["items"],
        "costs": {
            "committed_usd": costs["committed_usd"],
            "confirmed_usd": costs["confirmed_usd"],
            "unknown_cost_count": costs["unknown_cost_count"],
        },
        "unresolved_jobs": unresolved,
    }
