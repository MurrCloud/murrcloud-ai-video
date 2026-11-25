"""Owner-scoped workspace library and explicit reusable-template controls."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.dependencies import current_user, editor, get_db
from app.generation import GenerationError
from app.library import (
    apply_template,
    archive_project,
    create_template,
    duplicate_project,
    project_list,
    template_detail,
    template_list,
    update_metadata,
    update_template,
)
from app.library_schemas import (
    ArchiveIn,
    DuplicateIn,
    MetadataIn,
    TemplateApplyIn,
    TemplateCreateIn,
    TemplatePatchIn,
)
from app.models import User
from app.schemas import ProjectOut

router = APIRouter(prefix="/api/v1", tags=["library"])


def fail(exc):
    raise HTTPException(exc.status, exc.code) from None


@router.get("/library/projects")
def projects(
    q: str = Query("", max_length=200),
    status: str | None = Query(None, max_length=32),
    archived: bool = False,
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        return project_list(
            db, user, query=q, status=status, archived=archived, limit=limit, offset=offset
        )
    except GenerationError as exc:
        fail(exc)


@router.post("/projects/{project_id}/duplicate", response_model=ProjectOut, status_code=201)
def duplicate(
    project_id: UUID,
    payload: DuplicateIn,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        return duplicate_project(db, project_id, user, payload)
    except GenerationError as exc:
        fail(exc)


@router.patch("/projects/{project_id}/metadata", response_model=ProjectOut)
def metadata(
    project_id: UUID,
    payload: MetadataIn,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        return update_metadata(db, project_id, user, payload)
    except GenerationError as exc:
        fail(exc)


@router.post("/projects/{project_id}/archive", response_model=ProjectOut)
def archive(
    project_id: UUID,
    payload: ArchiveIn,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        return archive_project(db, project_id, user, payload)
    except GenerationError as exc:
        fail(exc)


@router.get("/templates")
def templates(
    q: str = Query("", max_length=200),
    archived: bool = False,
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        return template_list(db, user, query=q, archived=archived, limit=limit, offset=offset)
    except GenerationError as exc:
        fail(exc)


@router.get("/templates/{template_id}")
def template(template_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        return template_detail(db, template_id, user)
    except GenerationError as exc:
        fail(exc)


@router.post("/projects/{project_id}/templates", status_code=201)
def save_template(
    project_id: UUID,
    payload: TemplateCreateIn,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        return create_template(db, project_id, user, payload)
    except GenerationError as exc:
        fail(exc)


@router.patch("/templates/{template_id}")
def edit_template(
    template_id: UUID,
    payload: TemplatePatchIn,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        return update_template(db, template_id, user, payload)
    except GenerationError as exc:
        fail(exc)


@router.post("/templates/{template_id}/apply", response_model=ProjectOut, status_code=201)
def apply(
    template_id: UUID,
    payload: TemplateApplyIn,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        return apply_template(db, template_id, user, payload)
    except GenerationError as exc:
        fail(exc)
