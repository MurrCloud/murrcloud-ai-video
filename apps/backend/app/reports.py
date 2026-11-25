"""Database-aggregated accounting: reservations, confirmed charges and unknowns differ."""

from decimal import Decimal

from sqlalchemy import Numeric, String, Uuid, and_, case, func, literal, select, union_all

from app.accounting import spending_exposure_for_scope
from app.generation import json_value, owned_project
from app.models import (
    ActualUsageCost,
    DirectorJob,
    GenerationJob,
    Project,
    Scene,
    SceneVersion,
    VideoModel,
)

ZERO = literal(Decimal("0"), type_=Numeric(14, 6))


def project_scope(user, project_id=None):
    query = select(Project.id)
    if user.role != "admin":
        query = query.where(Project.owner_id == user.id)
    if project_id:
        query = query.where(Project.id == project_id)
    return query


def ledger_for(user, project_id=None):
    scope = project_scope(user, project_id)
    # A reservation is counted once per job, irrespective of charge-row count.
    generation = (
        select(
            GenerationJob.project_id.label("project_id"),
            Project.name.label("project_name"),
            Scene.id.label("scene_id"),
            Scene.scene_key.label("scene_key"),
            GenerationJob.model_id.label("model"),
            VideoModel.display_name.label("model_name"),
            GenerationJob.provider.label("provider"),
            GenerationJob.reserved_cost_usd.label("reserved_usd"),
            ZERO.label("confirmed_usd"),
            literal(0).label("known_count"),
            literal(0).label("unknown_count"),
            literal(0).label("pending_count"),
            literal(1).label("generation_jobs"),
            literal(0).label("director_jobs"),
        )
        .join(Project, Project.id == GenerationJob.project_id)
        .join(SceneVersion, SceneVersion.id == GenerationJob.scene_version_id)
    )
    generation = (
        generation.join(Scene, Scene.id == SceneVersion.scene_id)
        .join(VideoModel, VideoModel.model_id == GenerationJob.model_id)
        .where(GenerationJob.project_id.in_(scope))
    )
    director = (
        select(
            DirectorJob.project_id,
            Project.name,
            literal(None, type_=Uuid()),
            literal(None, type_=String()),
            DirectorJob.model,
            DirectorJob.model,
            literal("openai"),
            DirectorJob.reserved_cost_usd,
            ZERO,
            literal(0),
            literal(0),
            literal(0),
            literal(0),
            literal(1),
        )
        .join(Project, Project.id == DirectorJob.project_id)
        .where(DirectorJob.project_id.in_(scope))
    )
    known = and_(ActualUsageCost.status == "confirmed", ActualUsageCost.amount_usd.is_not(None))
    costs = select(
        ActualUsageCost.project_id,
        Project.name,
        Scene.id,
        Scene.scene_key,
        func.coalesce(
            ActualUsageCost.model_id, GenerationJob.model_id, DirectorJob.model, "unassigned"
        ),
        func.coalesce(
            VideoModel.display_name,
            DirectorJob.model,
            ActualUsageCost.model_id,
            GenerationJob.model_id,
            "Unassigned model",
        ),
        ActualUsageCost.provider,
        ZERO,
        case((known, ActualUsageCost.amount_usd), else_=ZERO),
        case((known, 1), else_=0),
        case(
            (ActualUsageCost.status == "unavailable", 1),
            (and_(ActualUsageCost.amount_usd.is_(None), ActualUsageCost.status != "pending"), 1),
            else_=0,
        ),
        case((ActualUsageCost.status == "pending", 1), else_=0),
        literal(0),
        literal(0),
    ).join(Project, Project.id == ActualUsageCost.project_id)
    costs = costs.outerjoin(GenerationJob, GenerationJob.id == ActualUsageCost.generation_job_id)
    costs = costs.outerjoin(DirectorJob, DirectorJob.id == ActualUsageCost.director_job_id)
    costs = costs.outerjoin(
        SceneVersion,
        SceneVersion.id
        == func.coalesce(ActualUsageCost.scene_version_id, GenerationJob.scene_version_id),
    )
    costs = costs.outerjoin(Scene, Scene.id == SceneVersion.scene_id)
    costs = costs.outerjoin(
        VideoModel,
        VideoModel.model_id == func.coalesce(ActualUsageCost.model_id, GenerationJob.model_id),
    )
    costs = costs.where(ActualUsageCost.project_id.in_(scope))
    return union_all(generation, director, costs).subquery("usage_ledger")


def aggregate_columns(ledger):
    return [
        func.coalesce(func.sum(ledger.c[field]), 0).label(field)
        for field in (
            "reserved_usd",
            "confirmed_usd",
            "known_count",
            "unknown_count",
            "pending_count",
            "generation_jobs",
            "director_jobs",
        )
    ]


def accounting_values(row):
    known = int(row["known_count"])
    confirmed = row["confirmed_usd"] if known else None
    return {
        "reserved_usd": row["reserved_usd"],
        "confirmed_usd": confirmed,
        "total_actual_usd": confirmed
        if known and not row["unknown_count"] and not row["pending_count"]
        else None,
        "known_cost_count": known,
        "unknown_cost_count": int(row["unknown_count"]),
        "pending_cost_count": int(row["pending_count"]),
        "job_count": int(row["generation_jobs"] + row["director_jobs"]),
        "generation_jobs": int(row["generation_jobs"]),
        "director_jobs": int(row["director_jobs"]),
    }


def usage_report(db, user, *, group_by="project", project_id=None, limit=50, offset=0):
    if project_id:
        owned_project(db, project_id, user)
    ledger = ledger_for(user, project_id)
    summary = db.execute(select(*aggregate_columns(ledger))).mappings().one()
    projects = db.execute(
        select(
            func.count(Project.id),
            func.coalesce(func.sum(Project.budget_usd), 0),
            func.coalesce(func.sum(Project.approved_cap_usd), 0),
            func.coalesce(func.sum(Project.committed_cost_usd), 0),
        ).where(Project.id.in_(project_scope(user, project_id)))
    ).one()
    exposure = spending_exposure_for_scope(db, project_scope(user, project_id), projects[3])
    totals = {
        **accounting_values(summary),
        "project_count": projects[0],
        "budget_usd": projects[1],
        "approved_cap_usd": projects[2],
        "committed_usd": projects[3],
        "exposure_usd": exposure,
        "remaining_budget_usd": projects[1] - exposure,
        "remaining_approved_usd": projects[2] - exposure,
    }
    key = {
        "project": ledger.c.project_id,
        "scene": ledger.c.scene_id,
        "model": ledger.c.model,
        "provider": ledger.c.provider,
    }[group_by]
    groups = select(
        key.label("key"),
        func.max(ledger.c.project_name).label("project_name"),
        func.max(ledger.c.scene_key).label("scene_key"),
        func.max(ledger.c.model_name).label("model_name"),
        *aggregate_columns(ledger),
    ).group_by(key)
    total = db.scalar(select(func.count()).select_from(groups.subquery()))
    rows = (
        db.execute(groups.order_by(key.asc().nulls_first()).offset(offset).limit(limit))
        .mappings()
        .all()
    )
    items = []
    for row in rows:
        identifier = row["key"]
        if group_by == "project":
            label = row["project_name"]
        elif group_by == "scene":
            label = (
                f"{row['project_name']} · {row['scene_key']}"
                if identifier
                else "Project-wide / unassigned"
            )
        elif group_by == "model":
            label = row["model_name"]
        else:
            label = identifier or "Unassigned provider"
        items.append(
            {
                "key": str(identifier) if identifier is not None else "unassigned",
                "label": label,
                "project_id": identifier if group_by == "project" else project_id,
                "scene_id": identifier if group_by == "scene" else None,
                **accounting_values(row),
            }
        )
    return json_value(
        {
            "currency": "USD",
            "basis": "cumulative_including_archived",
            "scope": "all" if user.role == "admin" else "owned",
            "totals": totals,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
