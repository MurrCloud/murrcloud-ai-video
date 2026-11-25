"""Real PostgreSQL planning races, isolated inside a disposable random schema."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app.director import DirectorPlan, DirectorResult
from app.director_tasks import plan_storyboard
from app.generation import GenerationError
from app.models import (
    CostEstimate,
    DirectorJob,
    GenerationJob,
    Project,
    Storyboard,
    StoryboardRevision,
    User,
)
from app.storyboard import (
    approve_board,
    board_row,
    create_director_job,
    editor_payload,
    estimate_board,
    save_board,
    save_input,
)
from app.storyboard_schemas import (
    BoardApprovalIn,
    BoardEstimateIn,
    DirectorApprovalIn,
    StoryboardPut,
)
from pydantic import SecretStr
from sqlalchemy import func, select
from test_generation_postgres import LocalQuote
from test_generation_postgres import postgres_generation as postgres_generation
from test_storyboard import WAN, scene


@pytest.fixture
def pg_board(postgres_generation):
    fixture = postgres_generation
    settings = fixture.settings.model_copy(
        update={
            "director_enabled": True,
            "openai_key": SecretStr("offline-only-openai-key"),
            "director_model": "offline-pg-director",
            "director_approval_reference": "offline-no-live-approval",
        }
    )
    with fixture.sessions() as db:
        project = db.get(Project, fixture.project_id)
        project.budget_usd = Decimal("10")
        project.target_duration_seconds = 10
        db.commit()
        user = db.get(User, fixture.user_id)
        source = save_input(db, project, user, "Offline PostgreSQL planning input", "script")
        board = save_board(
            db,
            project.id,
            user,
            StoryboardPut(
                expected_revision=1,
                title="Original plan",
                target_duration_seconds=10,
                language="en",
                visual_style="cinematic",
                target_audience="",
                default_model_id=WAN,
                scenes=[scene(1), scene(2)],
            ),
        )
        source_id = source.id
        revision = board.revision
    return SimpleNamespace(
        **vars(fixture), settings_phase3=settings, source_id=source_id, revision=revision
    )


def quote(fixture):
    with fixture.sessions() as db:
        return asyncio.run(
            estimate_board(
                db,
                fixture.project_id,
                db.get(User, fixture.user_id),
                BoardEstimateIn(expected_revision=fixture.revision),
                fixture.settings_phase3,
                LocalQuote(),
            )
        ).id


@pytest.mark.integration
def test_concurrent_editor_updates_keep_one_revision(pg_board):
    fixture = pg_board
    barrier = threading.Barrier(2)
    with fixture.sessions() as db:
        payload = editor_payload(
            db,
            db.get(Project, fixture.project_id),
            board_row(db, db.get(Project, fixture.project_id)),
        )

    def edit(title):
        with fixture.sessions() as db:
            user = db.get(User, fixture.user_id)
            barrier.wait(timeout=10)
            try:
                return save_board(
                    db, fixture.project_id, user, payload.model_copy(update={"title": title})
                ).revision
            except GenerationError as exc:
                db.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(edit, ["First concurrent edit", "Second concurrent edit"]))
    assert results.count("revision_conflict") == 1 and results.count(fixture.revision + 1) == 1
    with fixture.sessions() as db:
        assert db.scalar(select(Storyboard)).revision == fixture.revision + 1
        assert db.scalar(select(func.count()).select_from(StoryboardRevision)) == 3


@pytest.mark.integration
def test_late_plan_child_failure_has_no_partial_postgres_budget_or_jobs(pg_board):
    fixture = pg_board
    identifier = quote(fixture)
    from app.models import StoryboardEstimate

    with fixture.sessions() as db:
        row = db.get(StoryboardEstimate, identifier)
        first = UUID(row.snapshot["scenes"][0]["estimate_id"])
        second = UUID(row.snapshot["scenes"][1]["estimate_id"])
        db.get(CostEstimate, second).plan_hash = "invalidated-offline-fixture"
        db.commit()
        with pytest.raises(GenerationError, match="estimate_changed"):
            approve_board(
                db,
                fixture.project_id,
                db.get(User, fixture.user_id),
                BoardApprovalIn(
                    estimate_id=identifier,
                    expected_revision=fixture.revision,
                    idempotency_key=str(uuid4()),
                    approved_cap_usd=Decimal("2"),
                ),
                fixture.settings_phase3,
            )
    with fixture.sessions() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.get(Project, fixture.project_id).committed_cost_usd == 0
        assert db.get(CostEstimate, first).status == "pending"


@pytest.mark.integration
def test_director_result_cannot_overwrite_concurrent_postgres_revision(pg_board):
    fixture = pg_board
    with fixture.sessions() as db:
        identifier = create_director_job(
            db,
            fixture.project_id,
            db.get(User, fixture.user_id),
            DirectorApprovalIn(
                input_id=fixture.source_id,
                expected_revision=fixture.revision,
                idempotency_key=str(uuid4()),
                max_cost_usd=Decimal("0.1"),
                approve_unknown_price=True,
            ),
            fixture.settings_phase3,
        ).id

    class OfflineDirector:
        async def generate(self, request, *, approved=False):
            assert approved
            # Uses another real connection while the paid-intent transaction is
            # already committed, without contacting an external provider.
            with fixture.sessions() as db:
                board = db.scalar(select(Storyboard))
                board.revision += 1
                board.title = "Independent later PostgreSQL revision"
                db.commit()
            return DirectorResult(
                DirectorPlan.model_validate(
                    {"title": "Stale AI output", "scenes": [scene(1), scene(2)]}
                ),
                "resp_offline_pg_" + uuid4().hex,
                {"input_tokens": 10, "output_tokens": 20},
            )

        async def aclose(self):
            pass

    plan_storyboard(
        identifier,
        sessions=fixture.sessions,
        settings=fixture.settings_phase3,
        director_factory=lambda _: OfflineDirector(),
        dispatcher=lambda *_, **__: None,
        admission=lambda _: True,
    )
    with fixture.sessions() as db:
        board = db.scalar(select(Storyboard))
        assert (
            board.title == "Independent later PostgreSQL revision"
            and board.revision == fixture.revision + 1
        )
        assert db.get(DirectorJob, identifier).status == "stale"
        assert db.get(Project, fixture.project_id).committed_cost_usd == Decimal("0.1")
