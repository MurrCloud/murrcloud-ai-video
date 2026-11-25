"""Owner-scoped manual planning remains available with every paid feature disabled."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.dependencies import current_user, editor, get_db
from app.director_tasks import dispatch_director
from app.generation import GenerationError, dispatch_job, json_value, owned_project
from app.models import DirectorJob, ProjectInput, User
from app.storyboard import (
    approval_response,
    approve_board,
    board_row,
    create_director_job,
    director_response,
    estimate_board,
    estimate_response,
    read_board,
    save_board,
    save_input,
    validate_board,
)
from app.storyboard_schemas import (
    BoardApprovalIn,
    BoardEstimateIn,
    DirectorApprovalIn,
    InputIn,
    RevisionIn,
    StoryboardPut,
)

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["storyboard"])


def fail(exc):
    raise HTTPException(exc.status, exc.code) from None


@router.get("/storyboard")
def get_board(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        return json_value(read_board(db, owned_project(db, project_id, user)))
    except GenerationError as exc:
        fail(exc)


@router.put("/storyboard")
def put_board(
    project_id: UUID,
    payload: StoryboardPut,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        board = save_board(db, project_id, user, payload)
        return json_value(read_board(db, owned_project(db, project_id, user), board))
    except GenerationError as exc:
        fail(exc)


@router.get("/inputs")
def get_inputs(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        project = owned_project(db, project_id, user)
        board = board_row(db, project)
        current_input_id = board.input_id if board else None
        rows = db.scalars(
            select(ProjectInput)
            .where(ProjectInput.project_id == project_id)
            .options(
                load_only(
                    ProjectInput.id,
                    ProjectInput.kind,
                    ProjectInput.source_name,
                    ProjectInput.sha256,
                    ProjectInput.warnings,
                    ProjectInput.created_at,
                )
            )
            .order_by(ProjectInput.created_at.desc(), ProjectInput.id)
            .limit(20)
        ).all()
        if current_input_id and current_input_id not in {row.id for row in rows}:
            current = db.scalar(
                select(ProjectInput).where(
                    ProjectInput.id == current_input_id, ProjectInput.project_id == project_id
                )
            )
            if current:
                rows = [current, *rows]
        return json_value(
            [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "text": row.text if row.id == current_input_id else "",
                    "source_name": row.source_name,
                    "sha256": row.sha256,
                    "warnings": row.warnings,
                    "created_at": row.created_at,
                }
                for row in rows
            ]
        )
    except GenerationError as exc:
        fail(exc)


@router.post("/inputs", status_code=201)
def put_input(
    project_id: UUID, payload: InputIn, user: User = Depends(editor), db: Session = Depends(get_db)
):
    try:
        project = owned_project(db, project_id, user)
        row = save_input(db, project, user, payload.text, payload.kind, payload.source_name)
        return json_value(
            {
                "id": row.id,
                "kind": row.kind,
                "text": row.text,
                "source_name": row.source_name,
                "sha256": row.sha256,
                "warnings": row.warnings,
                "created_at": row.created_at,
                "storyboard_revision": board_row(db, project).revision,
            }
        )
    except GenerationError as exc:
        fail(exc)


@router.post("/storyboard/validate")
async def validate(
    project_id: UUID,
    payload: RevisionIn,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    provider = request.app.state.provider_factory(request.app.state.settings)
    try:
        return json_value(
            await validate_board(
                db,
                project_id,
                user,
                payload.expected_revision,
                request.app.state.settings,
                provider,
            )
        )
    except GenerationError as exc:
        fail(exc)
    finally:
        await provider.aclose()


@router.post("/storyboard/estimate", status_code=201)
async def estimate(
    project_id: UUID,
    payload: BoardEstimateIn,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    provider = request.app.state.provider_factory(request.app.state.settings)
    try:
        return json_value(
            estimate_response(
                await estimate_board(
                    db, project_id, user, payload, request.app.state.settings, provider
                )
            )
        )
    except GenerationError as exc:
        fail(exc)
    finally:
        await provider.aclose()


@router.post("/storyboard/approve", status_code=202)
def approve(
    project_id: UUID,
    payload: BoardApprovalIn,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        row = approve_board(db, project_id, user, payload, request.app.state.settings)
        for identifier in row.generation_job_ids:
            dispatch_job(
                db,
                UUID(identifier),
                request.app.state.settings,
                request.app.state.generation_dispatcher,
            )
        return json_value(approval_response(db, row))
    except GenerationError as exc:
        fail(exc)


@router.post("/director/jobs", status_code=202)
def director(
    project_id: UUID,
    payload: DirectorApprovalIn,
    request: Request,
    user: User = Depends(editor),
    db: Session = Depends(get_db),
):
    try:
        row = create_director_job(db, project_id, user, payload, request.app.state.settings)
        dispatch_director(
            db, row.id, request.app.state.settings, request.app.state.director_dispatcher
        )
        return json_value(director_response(row))
    except GenerationError as exc:
        fail(exc)


@router.get("/director/jobs")
def directors(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        owned_project(db, project_id, user)
        rows = db.scalars(
            select(DirectorJob)
            .where(DirectorJob.project_id == project_id)
            .order_by(DirectorJob.created_at.desc(), DirectorJob.id)
            .limit(100)
        ).all()
        return json_value([director_response(row) for row in rows])
    except GenerationError as exc:
        fail(exc)
