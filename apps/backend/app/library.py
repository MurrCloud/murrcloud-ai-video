"""Bounded workspace discovery and atomic, media-free project/template reuse."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, or_, select

from app.accounting import spending_exposure
from app.generation import TERMINAL, GenerationError, fingerprint, json_value, owned_project
from app.library_schemas import PortablePlan, PortableSummary
from app.models import (
    AuditEvent,
    DirectorJob,
    GenerationJob,
    MediaAsset,
    Project,
    ProjectInput,
    ProjectSettings,
    ProjectTemplate,
    RenderingJob,
    Scene,
    Storyboard,
    User,
    VideoModel,
    WorkspaceMutation,
)
from app.security import aware_utc
from app.storyboard import board_row, read_board, save_board
from app.storyboard_schemas import StoryboardPut

PROJECT_STATES = {
    "draft",
    "planning",
    "awaiting_approval",
    "generating",
    "clips_ready",
    "narrating",
    "rendering",
    "completed",
    "failed",
    "cancelled",
}


def _scope(user):
    return [] if user.role == "admin" else [Project.owner_id == user.id]


def _search(column, query):
    # Wildcards are data, never a way to bypass the owner/archive predicates.
    return func.lower(column).contains(query.lower(), autoescape=True)


def project_list(db, user, *, query="", status=None, archived=False, limit=24, offset=0):
    if status and status not in PROJECT_STATES:
        raise GenerationError("invalid_project_status", 422)
    predicates = [*_scope(user), Project.archived.is_(archived)]
    if query:
        predicates.append(or_(_search(Project.name, query), _search(Project.prompt, query)))
    if status:
        predicates.append(Project.status == status)
    total = db.scalar(select(func.count()).select_from(Project).where(*predicates))
    count = (
        select(func.count())
        .select_from(Scene)
        .where(Scene.project_id == Project.id, Scene.archived.is_(False))
        .correlate(Project)
        .scalar_subquery()
    )
    fields = [
        Project.id,
        Project.owner_id,
        Project.name,
        func.substr(Project.prompt, 1, 500).label("prompt"),
        (func.length(Project.prompt) > 500).label("prompt_truncated"),
        Project.status,
        Project.target_duration_seconds,
        Project.budget_usd,
        Project.approved_cap_usd,
        Project.committed_cost_usd,
        Project.archived,
        Project.created_at,
        Project.updated_at,
        count.label("scene_count"),
    ]
    rows = (
        db.execute(
            select(*fields)
            .where(*predicates)
            .order_by(Project.updated_at.desc(), Project.id)
            .offset(offset)
            .limit(limit)
        )
        .mappings()
        .all()
    )
    identifiers = [row["id"] for row in rows]
    outputs = {}
    if identifiers:
        ranked = select(
            RenderingJob.project_id,
            RenderingJob.id.label("render_id"),
            RenderingJob.output_asset_id.label("id"),
            RenderingJob.composition_snapshot["source"]["revision"]
            .as_integer()
            .label("source_revision"),
            RenderingJob.composition_snapshot["source"]["target_duration_seconds"]
            .as_integer()
            .label("source_duration"),
            func.row_number()
            .over(
                partition_by=RenderingJob.project_id,
                order_by=[RenderingJob.created_at.desc(), RenderingJob.id],
            )
            .label("ranking"),
        )
        ranked = (
            ranked.join(MediaAsset, MediaAsset.id == RenderingJob.output_asset_id)
            .where(
                RenderingJob.project_id.in_(identifiers),
                RenderingJob.status == "succeeded",
                MediaAsset.project_id == RenderingJob.project_id,
                MediaAsset.kind == "final_video",
                MediaAsset.status == "ready",
            )
            .subquery()
        )
        revisions = dict(
            db.execute(
                select(Storyboard.project_id, Storyboard.revision).where(
                    Storyboard.project_id.in_(identifiers)
                )
            ).all()
        )
        durations = {row["id"]: row["target_duration_seconds"] for row in rows}
        for row in db.execute(select(ranked).where(ranked.c.ranking == 1)).mappings():
            outputs[row["project_id"]] = {
                "id": row["id"],
                "render_id": row["render_id"],
                "preview_url": f"/api/v1/assets/{row['id']}/preview",
                "download_url": f"/api/v1/assets/{row['id']}/download",
                "is_current": row["source_revision"] == revisions.get(row["project_id"], 0)
                and row["source_duration"] == durations[row["project_id"]],
            }
    return json_value(
        {
            "items": [{**row, "latest_output": outputs.get(row["id"])} for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


def _actor_mutation(db, user, operation, key, payload):
    actor = db.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not actor or not actor.active or actor.role not in {"editor", "admin"}:
        raise GenerationError("editor_unavailable", 403)
    digest = fingerprint(payload)
    existing = db.scalar(
        select(WorkspaceMutation).where(
            WorkspaceMutation.actor_user_id == actor.id,
            WorkspaceMutation.operation == operation,
            WorkspaceMutation.idempotency_key == key,
        )
    )
    if existing and existing.request_fingerprint != digest:
        raise GenerationError("idempotency_conflict")
    return actor, digest, existing


def _record_mutation(db, actor, operation, key, digest, *, project=None, template=None):
    db.add(
        WorkspaceMutation(
            actor_user_id=actor.id,
            operation=operation,
            idempotency_key=key,
            request_fingerprint=digest,
            result_project_id=project.id if project else None,
            result_template_id=template.id if template else None,
        )
    )


def _validate_models(db, plan):
    identifiers = set(plan.summary.model_ids)
    available = (
        set(
            db.scalars(
                select(VideoModel.model_id).where(VideoModel.model_id.in_(identifiers))
            ).all()
        )
        if identifiers
        else set()
    )
    if available != identifiers:
        raise GenerationError("template_model_not_found", 422)


def _portable_plan(db, project):
    source_settings = db.scalar(
        select(ProjectSettings).where(ProjectSettings.project_id == project.id)
    )
    if not source_settings:
        raise GenerationError("project_settings_missing", 422)
    board = board_row(db, project)
    value = read_board(db, project, board)
    composition = source_settings.composition or {}
    production = composition.get("production")
    if production is not None:
        from app.production_schemas import ProductionSettings

        # Parse known settings first and explicitly strip portable approvals/media.
        try:
            production = ProductionSettings.model_validate(production).model_dump(mode="json")
        except (ValueError, TypeError):
            raise GenerationError("project_settings_invalid", 422) from None
        production.update(music_asset_id=None, watermark_asset_id=None, allow_upscale=False)
    source = db.get(ProjectInput, board.input_id) if board and board.input_id else None
    if source and source.project_id != project.id:
        raise GenerationError("project_input_not_found", 422)
    payload = {
        "prompt": project.prompt,
        "title": value["title"],
        "target_duration_seconds": project.target_duration_seconds,
        "source": {"kind": "prompt" if source.kind == "prompt" else "script", "text": source.text}
        if source
        else None,
        "settings": {
            "aspect_ratio": source_settings.aspect_ratio,
            "resolution": source_settings.resolution,
            "language": source_settings.language,
            "visual_style": source_settings.visual_style,
            "generation_mode": source_settings.generation_mode,
            "default_model_id": source_settings.default_model_id,
            "narration_enabled": source_settings.narration_enabled,
            "voice": source_settings.voice,
            "speaking_rate": source_settings.speaking_rate,
            "target_audience": composition.get("target_audience", ""),
            "production": production,
        },
        "scenes": [
            {k: v for k, v in scene.items() if k not in {"id", "reference_asset_ids"}}
            for scene in value["scenes"]
        ],
    }
    try:
        plan = PortablePlan.model_validate(payload)
    except (ValidationError, TypeError):
        raise GenerationError("project_plan_invalid", 422) from None
    _validate_models(db, plan)
    return plan


def _new_project(db, actor, name, budget, plan):
    _validate_models(db, plan)
    source_settings = plan.settings
    project = Project(
        owner_id=actor.id,
        name=name,
        prompt=plan.prompt,
        target_duration_seconds=plan.target_duration_seconds,
        budget_usd=budget,
        status="draft",
        committed_cost_usd=Decimal("0"),
        approved_cap_usd=None,
        archived=False,
    )
    db.add(project)
    db.flush()
    composition = {"target_audience": source_settings.target_audience}
    if source_settings.production is not None:
        composition["production"] = source_settings.production.model_dump(mode="json")
    db.add(
        ProjectSettings(
            project_id=project.id,
            aspect_ratio=source_settings.aspect_ratio,
            resolution=source_settings.resolution,
            language=source_settings.language,
            visual_style=source_settings.visual_style,
            generation_mode=source_settings.generation_mode,
            default_model_id=source_settings.default_model_id,
            narration_enabled=source_settings.narration_enabled,
            voice=source_settings.voice,
            speaking_rate=source_settings.speaking_rate,
            composition=composition,
        )
    )
    db.flush()
    board = board_row(db, project, create=True)
    if plan.source:
        source = ProjectInput(
            project_id=project.id,
            created_by_id=actor.id,
            kind=plan.source.kind,
            text=plan.source.text,
            source_name="Copied script",
            sha256=hashlib.sha256(plan.source.text.encode()).hexdigest(),
            warnings=[],
        )
        db.add(source)
        db.flush()
        board.input_id = source.id
        db.flush()
    payload = StoryboardPut(
        expected_revision=0,
        title=plan.title,
        target_duration_seconds=plan.target_duration_seconds,
        language=source_settings.language,
        visual_style=source_settings.visual_style,
        target_audience=source_settings.target_audience,
        default_model_id=source_settings.default_model_id,
        scenes=[scene.model_dump(mode="python") for scene in plan.scenes],
    )
    save_board(db, project.id, actor, payload, commit=False)
    return project


def duplicate_project(db, project_id, user, payload):
    actor, digest, existing = _actor_mutation(
        db,
        user,
        "project.duplicate",
        payload.idempotency_key,
        {"source_project_id": project_id, **payload.model_dump(mode="json")},
    )
    if existing:
        return owned_project(db, existing.result_project_id, actor)
    source = owned_project(db, project_id, actor, lock=True)
    if source.budget_usd > 10000:
        raise GenerationError("source_budget_outside_copy_limit", 422)
    plan = _portable_plan(db, source)
    project = _new_project(db, actor, payload.name, source.budget_usd, plan)
    _record_mutation(
        db, actor, "project.duplicate", payload.idempotency_key, digest, project=project
    )
    db.add(
        AuditEvent(
            actor_user_id=actor.id,
            project_id=project.id,
            event_type="project.duplicated",
            entity_type="project",
            entity_id=project.id,
            details={"source_project_id": str(project_id)},
        )
    )
    db.commit()
    return project


def _expect_updated(project, timestamp):
    if aware_utc(project.updated_at) != aware_utc(timestamp):
        raise GenerationError("project_metadata_conflict")


def update_metadata(db, project_id, user, payload):
    project = owned_project(db, project_id, user, lock=True)
    _expect_updated(project, payload.expected_updated_at)
    if project.archived:
        raise GenerationError("project_archived")
    if payload.budget_usd < max(
        spending_exposure(db, project), project.approved_cap_usd or Decimal("0")
    ):
        raise GenerationError("budget_below_existing_commitments")
    project.name, project.budget_usd = payload.name, payload.budget_usd
    project.updated_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            project_id=project.id,
            event_type="project.metadata_updated",
            entity_type="project",
            entity_id=project.id,
            details={"budget_usd": str(payload.budget_usd)},
        )
    )
    db.commit()
    return project


def archive_project(db, project_id, user, payload):
    project = owned_project(db, project_id, user, lock=True)
    _expect_updated(project, payload.expected_updated_at)
    if payload.archived:
        if db.scalar(
            select(GenerationJob.id)
            .where(GenerationJob.project_id == project_id, GenerationJob.status.not_in(TERMINAL))
            .limit(1)
        ) or db.scalar(
            select(DirectorJob.id)
            .where(
                DirectorJob.project_id == project_id,
                DirectorJob.status.in_(["approved", "submitting", "reconciliation_required"]),
            )
            .limit(1)
        ):
            raise GenerationError("unresolved_paid_jobs")
        if db.scalar(
            select(RenderingJob.id)
            .where(
                RenderingJob.project_id == project_id,
                RenderingJob.status.in_(["pending", "admitted", "running", "paused"]),
            )
            .limit(1)
        ):
            raise GenerationError("unresolved_render_jobs")
    project.archived = payload.archived
    project.updated_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            project_id=project.id,
            event_type="project.archived" if payload.archived else "project.unarchived",
            entity_type="project",
            entity_id=project.id,
            details={},
        )
    )
    db.commit()
    return project


def _template_scope(user):
    if user.role == "admin":
        return []
    return [
        or_(
            ProjectTemplate.owner_id == user.id,
            (ProjectTemplate.shared.is_(True) & ProjectTemplate.archived.is_(False)),
        )
    ]


def template_row(db, identifier, user, *, lock=False):
    query = select(ProjectTemplate).where(ProjectTemplate.id == identifier, *_template_scope(user))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if not row:
        raise GenerationError("template_not_found", 404)
    return row


def _template_data(row, user, summary, *, invalid=False):
    return {
        "id": row.id,
        "owner_id": row.owner_id,
        "name": row.name,
        "description": row.description,
        "shared": row.shared,
        "owned": row.owner_id == user.id,
        "revision": row.revision,
        "archived": row.archived,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "scene_count": summary.scene_count if summary else 0,
        "target_duration_seconds": summary.target_duration_seconds if summary else None,
        "language": summary.language if summary else None,
        "model_ids": summary.model_ids if summary else [],
        "plan_invalid": invalid,
    }


def template_list(db, user, *, query="", archived=False, limit=24, offset=0):
    predicates = [*_template_scope(user), ProjectTemplate.archived.is_(archived)]
    if query:
        predicates.append(
            or_(_search(ProjectTemplate.name, query), _search(ProjectTemplate.description, query))
        )
    total = db.scalar(select(func.count()).select_from(ProjectTemplate).where(*predicates))
    rows = db.execute(
        select(
            ProjectTemplate.id,
            ProjectTemplate.owner_id,
            ProjectTemplate.name,
            ProjectTemplate.description,
            ProjectTemplate.shared,
            ProjectTemplate.archived,
            ProjectTemplate.revision,
            ProjectTemplate.created_at,
            ProjectTemplate.updated_at,
            ProjectTemplate.production_plan["summary"].label("summary"),
        )
        .where(*predicates)
        .order_by(ProjectTemplate.updated_at.desc(), ProjectTemplate.id)
        .limit(limit)
        .offset(offset)
    ).all()
    values = []
    for row in rows:
        try:
            summary = PortableSummary.model_validate(row.summary)
        except (ValueError, TypeError):
            summary = None
        values.append(_template_data(row, user, summary, invalid=summary is None))
    return json_value({"items": values, "total": total, "limit": limit, "offset": offset})


def template_detail(db, identifier, user):
    row = template_row(db, identifier, user)
    try:
        plan = PortablePlan.model_validate(row.production_plan)
    except (ValueError, TypeError):
        raise GenerationError("template_plan_invalid", 422) from None
    return json_value(
        {**_template_data(row, user, plan.summary), "production_plan": plan.model_dump(mode="json")}
    )


def _sharing(user, shared, acknowledged):
    if shared and (user.role != "admin" or not acknowledged):
        raise GenerationError("shared_template_requires_admin_acknowledgement", 403)


def create_template(db, project_id, user, payload):
    actor, digest, existing = _actor_mutation(
        db,
        user,
        "template.create",
        payload.idempotency_key,
        {"source_project_id": project_id, **payload.model_dump(mode="json")},
    )
    if existing:
        return template_detail(db, existing.result_template_id, actor)
    _sharing(actor, payload.shared, payload.acknowledge_shared_content)
    source = owned_project(db, project_id, actor, lock=True)
    plan = _portable_plan(db, source)
    template = ProjectTemplate(
        owner_id=actor.id,
        name=payload.name,
        description=payload.description,
        shared=payload.shared,
        archived=False,
        revision=1,
        production_plan=plan.model_dump(mode="json"),
    )
    db.add(template)
    db.flush()
    _record_mutation(
        db, actor, "template.create", payload.idempotency_key, digest, template=template
    )
    db.add(
        AuditEvent(
            actor_user_id=actor.id,
            project_id=project_id,
            event_type="template.created",
            entity_type="project_template",
            entity_id=template.id,
            details={"shared": template.shared},
        )
    )
    db.commit()
    return template_detail(db, template.id, actor)


def update_template(db, identifier, user, payload):
    actor = db.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not actor or not actor.active or actor.role not in {"editor", "admin"}:
        raise GenerationError("editor_unavailable", 403)
    row = template_row(db, identifier, actor, lock=True)
    if row.owner_id != actor.id and actor.role != "admin":
        raise GenerationError("template_not_found", 404)
    if row.revision != payload.expected_revision:
        raise GenerationError("template_revision_conflict")
    if payload.shared is True or (row.shared and payload.shared is not False):
        _sharing(actor, True, payload.acknowledge_shared_content)
    for field in ("name", "description", "shared", "archived"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    row.revision += 1
    row.updated_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            actor_user_id=actor.id,
            event_type="template.updated",
            entity_type="project_template",
            entity_id=row.id,
            details={"shared": row.shared, "archived": row.archived, "revision": row.revision},
        )
    )
    db.commit()
    # Invalid legacy templates can be safely archived without exposing raw plans.
    try:
        summary = PortableSummary.model_validate(row.production_plan.get("summary"))
    except (ValueError, TypeError):
        summary = None
    return json_value(_template_data(row, actor, summary, invalid=summary is None))


def apply_template(db, identifier, user, payload):
    actor, digest, existing = _actor_mutation(
        db,
        user,
        "template.apply",
        payload.idempotency_key,
        {"template_id": identifier, **payload.model_dump(mode="json")},
    )
    if existing:
        return owned_project(db, existing.result_project_id, actor)
    template = template_row(db, identifier, actor, lock=True)
    if template.archived:
        raise GenerationError("template_archived")
    try:
        plan = PortablePlan.model_validate(template.production_plan)
    except (ValueError, TypeError):
        raise GenerationError("template_plan_invalid", 422) from None
    project = _new_project(db, actor, payload.name, payload.budget_usd, plan)
    _record_mutation(db, actor, "template.apply", payload.idempotency_key, digest, project=project)
    db.add(
        AuditEvent(
            actor_user_id=actor.id,
            project_id=project.id,
            event_type="template.applied",
            entity_type="project",
            entity_id=project.id,
            details={"template_id": str(identifier)},
        )
    )
    db.commit()
    return project


def list_projects(db, user, *, q="", status=None, archived=False, limit=24, offset=0):
    """Shared bounded DTO used by the dashboard and library routes."""
    return project_list(
        db, user, query=q, status=status, archived=archived, limit=limit, offset=offset
    )
