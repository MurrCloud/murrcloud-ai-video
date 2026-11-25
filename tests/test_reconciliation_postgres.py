"""Operator mutation races through the real API and isolated PostgreSQL row locks."""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.main import create_app
from app.models import (
    ActualUsageCost,
    AuditEvent,
    AuthSession,
    GenerationAttempt,
    GenerationJob,
    Project,
    Scene,
    SceneVersion,
    User,
    WorkspaceMutation,
)
from app.security import SESSION_COOKIE, aware_utc, new_session
from conftest import FakeLimiter
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event, func, select
from test_generation_postgres import postgres_generation as postgres_generation

WAN = "fal-ai/wan/v2.7/text-to-video"
ORIGIN = "http://localhost:3000"


@pytest.fixture
def pg_reconciliation(postgres_generation):
    fixture = postgres_generation
    config = fixture.settings.model_copy(
        update={
            "paid_generation_enabled": False,
            "paid_approval_reference": "",
            "fal_key": SecretStr(""),
            "cookie_secure": False,
            "allowed_origins": [ORIGIN],
            "api_requests_per_minute": 1000,
        }
    )
    external_calls = []

    def forbidden_external(*args, **kwargs):
        external_calls.append((args, kwargs))
        raise AssertionError("Reconciliation tests must never contact or dispatch providers")

    application = create_app(
        config,
        engine=fixture.engine,
        limiter=FakeLimiter(),
        provider_factory=forbidden_external,
        generation_dispatcher=forbidden_external,
        director_factory=forbidden_external,
        director_dispatcher=forbidden_external,
        production_dispatcher=forbidden_external,
    )
    tokens, admin_ids = [], []
    with fixture.sessions() as db:
        for number in range(2):
            admin = User(
                email=f"reconciliation-admin-{number}@example.com",
                password_hash="offline-only-never-used-for-authentication",
                role="admin",
            )
            db.add(admin)
            db.flush()
            token, digest, expires = new_session(config.session_secret.get_secret_value(), 1)
            db.add(AuthSession(user_id=admin.id, token_hash=digest, expires_at=expires))
            tokens.append(token)
            admin_ids.append(admin.id)
        project = db.get(Project, fixture.project_id)
        project.approved_cap_usd = Decimal("1")
        project.committed_cost_usd = Decimal("0.5")
        scene = Scene(
            project_id=project.id,
            scene_key="offline_reconciliation_scene",
            order_index=1,
            duration_seconds=5,
            video_model_id=WAN,
            visual_prompt="Offline reconciliation fixture",
        )
        db.add(scene)
        db.flush()
        version = SceneVersion(
            scene_id=scene.id,
            version_number=1,
            duration_seconds=5,
            generation_mode="text_to_video",
            video_model_id=WAN,
            visual_prompt=scene.visual_prompt,
        )
        db.add(version)
        db.flush()
        job = GenerationJob(
            project_id=project.id,
            scene_version_id=version.id,
            model_id=WAN,
            endpoint_id=WAN,
            provider="fal",
            idempotency_key=str(uuid4()),
            status="reconciliation_required",
            reserved_cost_usd=Decimal("0.5"),
        )
        db.add(job)
        db.flush()
        db.add(
            GenerationAttempt(
                job_id=job.id, attempt_number=1, operation="submit", status="ambiguous"
            )
        )
        cost = ActualUsageCost(
            project_id=project.id,
            generation_job_id=job.id,
            provider="fal",
            status="unavailable",
        )
        db.add(cost)
        db.commit()
        job_id, cost_id = job.id, cost.id

    def headers(actor=0):
        return {"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE}={tokens[actor]}"}

    def mutation_request(operation, *, key=None):
        with fixture.sessions() as db:
            if operation == "confirm_cost":
                db.get(GenerationJob, job_id).status = "failed"
                db.commit()
                row = db.get(ActualUsageCost, cost_id)
                path = f"/api/v1/operations/costs/{cost_id}/confirm"
                values = {"amount_usd": "0.347", "provider_charge_id": "offline-provider-bill"}
            else:
                row = db.get(GenerationJob, job_id)
                path = f"/api/v1/operations/reconciliations/generation/{job_id}"
                values = {"outcome": "verified_failed"}
            return path, {
                "expected_updated_at": aware_utc(row.updated_at).isoformat(),
                "idempotency_key": key or str(uuid4()),
                "reference": "offline-provider-verification",
                "acknowledge_verified": True,
                **values,
            }

    with TestClient(application, base_url="http://testserver") as client:
        yield SimpleNamespace(
            **vars(fixture),
            client=client,
            headers=headers,
            request=mutation_request,
            admin_ids=admin_ids,
            job_id=job_id,
            cost_id=cost_id,
            external_calls=external_calls,
        )
    assert external_calls == []


def assert_one_mutation(fixture, operation, event_type):
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
                .where(AuditEvent.event_type == event_type)
            )
            == 1
        )
        project = db.get(Project, fixture.project_id)
        assert project.committed_cost_usd == Decimal("0.5")
        assert project.approved_cap_usd == Decimal("1")
        assert db.scalar(select(GenerationAttempt.status)) == "ambiguous"


@pytest.mark.integration
@pytest.mark.parametrize(
    "operation,event_type",
    [
        ("reconcile_generation", "operator.verified_failed"),
        ("confirm_cost", "operator.charge_confirmed"),
    ],
)
def test_simultaneous_same_key_operator_retries_write_once(
    pg_reconciliation, operation, event_type
):
    fixture = pg_reconciliation
    path, payload = fixture.request(operation)
    barrier = threading.Barrier(2)

    def retry(_):
        barrier.wait(timeout=10)
        return fixture.client.post(path, headers=fixture.headers(), json=payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(retry, range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert_one_mutation(fixture, operation, event_type)


@pytest.mark.integration
def test_different_admin_keys_cannot_confirm_one_charge_twice(pg_reconciliation):
    fixture = pg_reconciliation
    path, payload = fixture.request("confirm_cost")
    barrier = threading.Barrier(2)

    def confirm(actor):
        barrier.wait(timeout=10)
        return fixture.client.post(
            path,
            headers=fixture.headers(actor),
            json={**payload, "idempotency_key": str(uuid4())},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(confirm, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert_one_mutation(fixture, "confirm_cost", "operator.charge_confirmed")
    with fixture.sessions() as db:
        row = db.get(ActualUsageCost, fixture.cost_id)
        assert row.status == "confirmed" and row.amount_usd == Decimal("0.347")


@pytest.mark.integration
@pytest.mark.parametrize("revocation", ["inactive", "demoted"])
def test_waiting_operator_mutation_rechecks_admin_after_actor_lock(pg_reconciliation, revocation):
    fixture = pg_reconciliation
    path, payload = fixture.request("confirm_cost")
    requested = threading.Event()

    def observe_actor_lock(_connection, _cursor, statement, _parameters, _context, _many):
        if "FOR UPDATE" in statement and "FROM users" in statement:
            requested.set()

    with fixture.sessions() as holder:
        actor = holder.scalar(select(User).where(User.id == fixture.admin_ids[0]).with_for_update())
        event.listen(fixture.engine, "before_cursor_execute", observe_actor_lock)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                response = executor.submit(
                    fixture.client.post, path, headers=fixture.headers(), json=payload
                )
                reached_lock = requested.wait(timeout=10)
                if reached_lock:
                    if revocation == "inactive":
                        actor.active = False
                    else:
                        actor.role = "editor"
                    holder.commit()
                else:
                    holder.rollback()
                assert reached_lock, "Request never reached the actor lock after authorization"
                result = response.result(timeout=10)
        finally:
            event.remove(fixture.engine, "before_cursor_execute", observe_actor_lock)
            holder.rollback()
    assert result.status_code == 403
    with fixture.sessions() as db:
        assert db.get(ActualUsageCost, fixture.cost_id).status == "unavailable"
        assert db.scalar(select(func.count()).select_from(WorkspaceMutation)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "operator.charge_confirmed")
            )
            == 0
        )
