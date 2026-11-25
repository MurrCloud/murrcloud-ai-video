"""Spending exposure retains reservations and adds verified charge overruns."""

from decimal import Decimal

from sqlalchemy import case, func, select

from app.models import ActualUsageCost, DirectorJob, GenerationJob, Project


def spending_exposure(db, project):
    return spending_exposure_for_scope(
        db, select(Project.id).where(Project.id == project.id), project.committed_cost_usd
    )


def spending_exposure_for_scope(db, scope, held):
    # A provider bill can exceed its estimate. Never release the held reserve or
    # raise an approval cap; count only the excess beyond each job's reserve.
    excess = Decimal("0")
    for model, cost_key in (
        (GenerationJob, ActualUsageCost.generation_job_id),
        (DirectorJob, ActualUsageCost.director_job_id),
    ):
        charges = (
            select(cost_key.label("job_id"), func.sum(ActualUsageCost.amount_usd).label("actual"))
            .where(
                ActualUsageCost.project_id.in_(scope),
                ActualUsageCost.status == "confirmed",
                ActualUsageCost.amount_usd.is_not(None),
                cost_key.is_not(None),
            )
            .group_by(cost_key)
            .subquery()
        )
        difference = charges.c.actual - model.reserved_cost_usd
        excess += db.scalar(
            select(func.coalesce(func.sum(case((difference > 0, difference), else_=0)), 0))
            .select_from(model)
            .join(charges, charges.c.job_id == model.id)
            .where(model.project_id.in_(scope))
        )
    standalone = db.scalar(
        select(func.coalesce(func.sum(ActualUsageCost.amount_usd), 0)).where(
            ActualUsageCost.project_id.in_(scope),
            ActualUsageCost.status == "confirmed",
            ActualUsageCost.generation_job_id.is_(None),
            ActualUsageCost.director_job_id.is_(None),
        )
    )
    return held + excess + standalone
