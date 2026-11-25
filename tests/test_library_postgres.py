"""Actual PostgreSQL mutation locks; a dedicated disposable test schema is required."""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from app.library import apply_template, create_template, duplicate_project
from app.library_schemas import DuplicateIn, TemplateApplyIn, TemplateCreateIn
from app.models import AuditEvent, Project, ProjectTemplate, Scene, User, WorkspaceMutation
from app.storyboard import save_board, save_input
from app.storyboard_schemas import StoryboardPut
from sqlalchemy import func, select
from test_generation_postgres import postgres_generation as postgres_generation
from test_storyboard import WAN, scene


@pytest.fixture
def pg_library(postgres_generation):
    fixture = postgres_generation
    with fixture.sessions() as db:
        project = db.get(Project, fixture.project_id)
        project.target_duration_seconds = 10
        db.commit()
        user = db.get(User, fixture.user_id)
        save_input(db, project, user, "Offline reusable script", "script")
        save_board(
            db,
            project.id,
            user,
            StoryboardPut(
                expected_revision=1,
                title="Reusable offline plan",
                target_duration_seconds=10,
                language="en",
                default_model_id=WAN,
                scenes=[scene(1), scene(2)],
            ),
        )
    return fixture


@pytest.mark.integration
@pytest.mark.parametrize(
    "operation,event_type",
    [
        ("project.duplicate", "project.duplicated"),
        ("template.create", "template.created"),
        ("template.apply", "template.applied"),
    ],
)
def test_concurrent_workspace_retries_create_one_result(pg_library, operation, event_type):
    fixture = pg_library
    key = str(uuid4())
    template_id = None
    if operation == "template.apply":
        with fixture.sessions() as db:
            result = create_template(
                db,
                fixture.project_id,
                db.get(User, fixture.user_id),
                TemplateCreateIn(name="Offline reusable template", idempotency_key=str(uuid4())),
            )
            template_id = UUID(result["id"])
    barrier = threading.Barrier(2)

    def retry(_):
        with fixture.sessions() as db:
            user = db.get(User, fixture.user_id)
            barrier.wait(timeout=10)
            if operation == "project.duplicate":
                return duplicate_project(
                    db,
                    fixture.project_id,
                    user,
                    DuplicateIn(name="Concurrent copy", idempotency_key=key),
                ).id
            if operation == "template.create":
                return create_template(
                    db,
                    fixture.project_id,
                    user,
                    TemplateCreateIn(name="Concurrent template", idempotency_key=key),
                )["id"]
            return apply_template(
                db,
                template_id,
                user,
                TemplateApplyIn(
                    name="Concurrent template instance",
                    budget_usd=Decimal("1"),
                    idempotency_key=key,
                ),
            ).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(retry, range(2)))
    assert results[0] == results[1]
    with fixture.sessions() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(WorkspaceMutation)
                .where(WorkspaceMutation.operation == operation)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.event_type == event_type,
                    AuditEvent.entity_id == UUID(str(results[0])),
                )
            )
            == 1
        )
        if operation == "template.create":
            assert db.scalar(select(func.count()).select_from(ProjectTemplate)) == 1
            assert db.scalar(select(func.count()).select_from(Project)) == 1
        else:
            assert db.scalar(select(func.count()).select_from(Project)) == 2
            assert db.scalar(select(func.count()).select_from(Scene)) == 4
            copied = db.get(Project, results[0])
            assert copied.committed_cost_usd == 0 and copied.approved_cap_usd is None
