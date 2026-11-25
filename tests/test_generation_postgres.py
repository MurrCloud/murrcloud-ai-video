"""Real row-lock acceptance, isolated in a random schema of the CI test DB."""

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.catalog import seed_catalog
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.generation import GenerationError, approve_estimate, create_estimate
from app.generation_schemas import GenerationApprovalIn, GenerationEstimateIn
from app.models import Base, GenerationJob, Project, ProjectSettings, User
from app.providers.base import CostQuote
from app.storage import StoredClip
from app.tasks import _poll_outcome
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url


class LocalQuote:
    async def validate_request(self, model, request):
        return {}

    async def estimate_cost(self, model, request):
        return CostQuote(Decimal("0.6"), "offline_account_fixture", True, {})


@pytest.fixture
def postgres_generation():
    url = os.environ.get("VIDEO_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set VIDEO_TEST_DATABASE_URL for isolated PostgreSQL concurrency tests")
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg" or not (
        (parsed.database or "") == "video_studio_test"
        or (parsed.database or "").startswith("video_studio_test_")
    ):
        pytest.fail("Refusing generation tests outside a dedicated video_studio_test database")
    schema = "video_generation_test_" + uuid4().hex
    admin = make_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=6,
        max_overflow=0,
        connect_args={
            "connect_timeout": 5,
            "options": f"-c search_path={schema} -c statement_timeout=15000",
        },
    )
    sessions = make_session_factory(engine)
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="sqlite://",
        redis_url="redis://localhost:6379/0",
        session_secret="offline-unit-session-" * 3,
        paid_generation_enabled=True,
        paid_approval_reference="offline-pg-tests-no-live-approval",
        fal_key="offline-only-no-live-provider-key",
    )
    try:
        Base.metadata.create_all(engine)
        with sessions() as db:
            seed_catalog(db)
            user = User(
                email="postgres-generation@example.com", password_hash="offline-only", role="editor"
            )
            db.add(user)
            db.flush()
            project = Project(
                owner_id=user.id, name="Concurrent approval test", budget_usd=Decimal("1")
            )
            db.add(project)
            db.flush()
            db.add(ProjectSettings(project_id=project.id))
            db.commit()
            user_id, project_id = user.id, project.id
        yield SimpleNamespace(
            sessions=sessions,
            settings=settings,
            user_id=user_id,
            project_id=project_id,
            engine=engine,
        )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def estimate(fixture):
    with fixture.sessions() as db:
        user = db.get(User, fixture.user_id)
        request = GenerationEstimateIn(
            model_id="fal-ai/wan/v2.7/text-to-video",
            prompt="Offline concurrency fixture",
            duration_seconds=5,
            mode="text_to_video",
            resolution="720p",
            aspect_ratio="16:9",
            audio_enabled=True,
        )
        row = asyncio.run(
            create_estimate(db, fixture.project_id, user, request, fixture.settings, LocalQuote())
        )
        return row.id


@pytest.mark.integration
def test_concurrent_reservations_cannot_exceed_project_cap(postgres_generation):
    fixture = postgres_generation
    identifiers = [estimate(fixture), estimate(fixture)]
    barrier = threading.Barrier(2)

    def approve(identifier):
        with fixture.sessions() as db:
            user = db.get(User, fixture.user_id)
            barrier.wait(timeout=10)
            try:
                job = approve_estimate(
                    db,
                    fixture.project_id,
                    user,
                    GenerationApprovalIn(
                        estimate_id=identifier,
                        idempotency_key=str(uuid4()),
                        approved_cap_usd=Decimal("1"),
                    ),
                    fixture.settings,
                )
                return "approved", job.id
            except GenerationError as exc:
                db.rollback()
                return exc.code, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(approve, identifiers))
    assert sorted(result[0] for result in results) == ["approved", "insufficient_approved_budget"]
    with fixture.sessions() as db:
        assert db.get(Project, fixture.project_id).committed_cost_usd == Decimal("0.6")
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1


@pytest.mark.integration
def test_concurrent_idempotent_approvals_create_one_job(postgres_generation):
    fixture = postgres_generation
    identifier = estimate(fixture)
    key = str(uuid4())
    barrier = threading.Barrier(2)

    def approve(_):
        with fixture.sessions() as db:
            user = db.get(User, fixture.user_id)
            barrier.wait(timeout=10)
            return approve_estimate(
                db,
                fixture.project_id,
                user,
                GenerationApprovalIn(
                    estimate_id=identifier, idempotency_key=key, approved_cap_usd=Decimal("1")
                ),
                fixture.settings,
            ).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(approve, range(2)))
    assert results[0] == results[1]
    with fixture.sessions() as db:
        assert db.get(Project, fixture.project_id).committed_cost_usd == Decimal("0.6")
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1


@pytest.mark.integration
def test_completion_cannot_overwrite_concurrent_approval_project_state(postgres_generation):
    fixture = postgres_generation
    with fixture.sessions() as db:
        db.get(Project, fixture.project_id).budget_usd = Decimal("2")
        db.commit()
    first_estimate = estimate(fixture)
    with fixture.sessions() as db:
        job = approve_estimate(
            db,
            fixture.project_id,
            db.get(User, fixture.user_id),
            GenerationApprovalIn(
                estimate_id=first_estimate,
                idempotency_key=str(uuid4()),
                approved_cap_usd=Decimal("1"),
            ),
            fixture.settings,
        )
        job_id = job.id
        token = uuid4()
        job.status = "running"
        job.operation_token = token
        db.commit()
    next_estimate = estimate(fixture)
    approval_owns_project = threading.Event()
    completion_waiting_for_project = threading.Event()

    def observe_lock(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            threading.current_thread().name == "generation-completion"
            and "FOR UPDATE" in statement
            and "FROM projects" in statement
        ):
            completion_waiting_for_project.set()

    event.listen(fixture.engine, "before_cursor_execute", observe_lock)

    def approve_while_worker_waits():
        with fixture.sessions() as db:
            user = db.get(User, fixture.user_id)
            db.scalar(select(Project).where(Project.id == fixture.project_id).with_for_update())
            approval_owns_project.set()
            assert completion_waiting_for_project.wait(timeout=10)
            return approve_estimate(
                db,
                fixture.project_id,
                user,
                GenerationApprovalIn(
                    estimate_id=next_estimate,
                    idempotency_key=str(uuid4()),
                    approved_cap_usd=Decimal("2"),
                ),
                fixture.settings,
            ).id

    def complete():
        threading.current_thread().name = "generation-completion"
        assert approval_owns_project.wait(timeout=10)
        return _poll_outcome(
            fixture.sessions,
            job_id,
            token,
            fixture.settings,
            asset=StoredClip(f"{fixture.project_id}/{job_id}.mp4", 128, "a" * 64),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            approval = executor.submit(approve_while_worker_waits)
            completion = executor.submit(complete)
            new_job_id = approval.result(timeout=15)
            completion.result(timeout=15)
    finally:
        event.remove(fixture.engine, "before_cursor_execute", observe_lock)
    with fixture.sessions() as db:
        assert db.get(GenerationJob, new_job_id).status == "approved"
        assert db.get(GenerationJob, job_id).status == "succeeded"
        assert db.get(Project, fixture.project_id).status == "generating"
