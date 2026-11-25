"""Offline director spending, durable intent, stale output and reference boundaries."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app.director import DirectorError, DirectorPlan, DirectorResult
from app.director_tasks import plan_storyboard, reconcile_directors
from app.models import (
    ActualUsageCost,
    DirectorAttempt,
    DirectorJob,
    MediaAsset,
    Project,
    Storyboard,
)
from pydantic import SecretStr
from sqlalchemy import func, select
from test_storyboard import ORIGIN, scene
from test_storyboard import board_studio as board_studio


class OfflineDirector:
    def __init__(self):
        self.calls = 0
        self.hook = None
        self.error = None
        self.plan = DirectorPlan.model_validate(
            {"title": "A new director plan", "scenes": [scene(1), scene(2)]}
        )

    async def generate(self, request, *, approved=False):
        assert approved
        self.calls += 1
        if self.hook:
            self.hook(request)
        if self.error:
            raise self.error
        return DirectorResult(
            self.plan,
            "resp_offline_" + uuid4().hex,
            {"input_tokens": 20, "output_tokens": 50, "total_tokens": 70},
        )

    async def aclose(self):
        pass


@pytest.fixture
def director_studio(board_studio):
    studio = board_studio
    client = OfflineDirector()
    settings = studio.settings.model_copy(
        update={
            "director_enabled": True,
            "openai_key": SecretStr("offline-only-openai-key"),
            "director_model": "offline-director-fixture",
            "director_approval_reference": "offline-no-live-approval",
        }
    )
    studio.api.app.state.settings = settings
    studio.api.app.state.director_factory = lambda _: client
    source = studio.input().json()
    studio.put([scene(1), scene(2)])
    url = studio.base + "/director/jobs"

    def approve(*, key=None, cap="0.05", ack=True, **changes):
        payload = {
            "input_id": source["id"],
            "expected_revision": studio.get()["revision"],
            "idempotency_key": key or str(uuid4()),
            "max_cost_usd": cap,
            "approve_unknown_price": ack,
            **changes,
        }
        return studio.api.client.post(url, json=payload, headers=ORIGIN)

    def worker(identifier, **changes):
        return plan_storyboard(
            identifier,
            sessions=studio.api.sessions,
            settings=settings,
            director_factory=lambda _: client,
            dispatcher=lambda *args, **kwargs: studio.queued.append((args, kwargs)),
            admission=changes.pop("admission", lambda _: True),
            **changes,
        )

    return SimpleNamespace(
        studio=studio,
        client=client,
        settings=settings,
        source=source,
        url=url,
        approve=approve,
        worker=worker,
    )


def test_director_gate_blocks_before_reservation_or_queue(director_studio):
    fixture = director_studio
    fixture.studio.api.app.state.settings = fixture.settings.model_copy(
        update={"director_enabled": False}
    )
    response = fixture.approve()
    assert response.status_code == 409 and response.json()["detail"] == "director_disabled"
    with fixture.studio.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(DirectorJob)) == 0
        assert db.get(Project, UUID(fixture.studio.project_id)).committed_cost_usd == 0
    assert not fixture.studio.queued and fixture.client.calls == 0


def test_unknown_director_price_requires_explicit_cap_and_ack(director_studio):
    fixture = director_studio
    assert fixture.approve(ack=False).status_code == 409
    assert fixture.approve(cap="11").status_code == 409
    response = fixture.approve()
    assert response.status_code == 202, response.text
    assert response.json()["actual_cost_usd"] is None
    assert Decimal(response.json()["reserved_cost_usd"]) == Decimal("0.05")
    with fixture.studio.api.sessions() as db:
        assert db.get(Project, UUID(fixture.studio.project_id)).committed_cost_usd == Decimal(
            "0.05"
        )
        assert db.scalar(select(ActualUsageCost)).amount_usd is None


def test_director_intent_is_durable_and_result_applies_as_new_revision(director_studio):
    fixture = director_studio
    approved = fixture.approve().json()
    before = fixture.studio.get()

    def inspect_intent(request):
        with fixture.studio.api.sessions() as db:
            assert db.get(DirectorJob, UUID(approved["id"])).status == "submitting"
            assert db.scalar(select(DirectorAttempt)).status == "started"
        assert request.script == "A script about calm water"
        assert request.target_duration_seconds == 10 and request.default_model_id

    fixture.client.hook = inspect_intent
    fixture.worker(approved["id"])
    fixture.worker(approved["id"])
    assert fixture.client.calls == 1
    after = fixture.studio.get()
    assert after["revision"] == before["revision"] + 1
    assert after["title"] == "A new director plan"
    jobs = fixture.studio.api.client.get(fixture.url).json()
    assert jobs[0]["status"] == "succeeded" and jobs[0]["result_revision"] == after["revision"]
    assert jobs[0]["usage"]["total_tokens"] == 70 and jobs[0]["actual_cost_usd"] is None


def test_director_approval_idempotency_and_unresolved_job_guard(director_studio):
    fixture = director_studio
    key = str(uuid4())
    first = fixture.approve(key=key)
    second = fixture.approve(key=key)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert fixture.approve().status_code == 409
    assert fixture.studio.put(fixture.studio.get()["scenes"]).status_code == 409
    assert (
        fixture.studio.input("Cannot replace while charged plan is unresolved").status_code == 409
    )
    with fixture.studio.api.sessions() as db:
        assert db.get(Project, UUID(fixture.studio.project_id)).committed_cost_usd == Decimal(
            "0.05"
        )


def test_director_worker_crash_never_automatically_repeats_paid_call(director_studio):
    fixture = director_studio

    class WorkerCrash(BaseException):
        pass

    def crash(_):
        raise WorkerCrash

    fixture.client.hook = crash
    identifier = fixture.approve().json()["id"]
    with pytest.raises(WorkerCrash):
        fixture.worker(identifier)
    fixture.client.hook = None
    fixture.worker(identifier)
    assert fixture.client.calls == 1
    with fixture.studio.api.sessions() as db:
        assert db.get(DirectorJob, UUID(identifier)).status == "reconciliation_required"
        assert db.get(Project, UUID(fixture.studio.project_id)).committed_cost_usd == Decimal(
            "0.05"
        )


@pytest.mark.parametrize("ambiguous", [True, False])
def test_director_failures_hold_budget_and_never_retry(director_studio, ambiguous):
    fixture = director_studio
    fixture.client.error = DirectorError(
        "director_refused",
        ambiguous=ambiguous,
        response_id="resp_failed_fixture",
        usage={"input_tokens": 20, "output_tokens": 3},
    )
    identifier = fixture.approve().json()["id"]
    fixture.worker(identifier)
    fixture.worker(identifier)
    assert fixture.client.calls == 1
    jobs = fixture.studio.api.client.get(fixture.url).json()
    assert jobs[0]["status"] == ("reconciliation_required" if ambiguous else "failed")
    assert jobs[0]["actual_cost_usd"] is None and jobs[0]["usage"]["input_tokens"] == 20


def test_director_host_admission_failure_creates_no_paid_attempt(director_studio):
    fixture = director_studio
    identifier = fixture.approve().json()["id"]
    fixture.worker(identifier, admission=lambda _: False)
    assert fixture.client.calls == 0
    with fixture.studio.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(DirectorAttempt)) == 0
        assert db.get(DirectorJob, UUID(identifier)).status == "approved"


def test_stale_director_completion_cannot_overwrite_current_board(director_studio):
    fixture = director_studio
    before = fixture.studio.get()

    def external_change(_):
        # Simulates an independent operator/configuration change, bypassing the
        # normal API guard, while the external request is in flight.
        with fixture.studio.api.sessions() as db:
            board = db.scalar(select(Storyboard))
            board.revision += 1
            board.title = "Independent later revision"
            db.commit()

    fixture.client.hook = external_change
    identifier = fixture.approve().json()["id"]
    fixture.worker(identifier)
    current = fixture.studio.get()
    assert (
        current["title"] == "Independent later revision"
        and current["revision"] == before["revision"] + 1
    )
    with fixture.studio.api.sessions() as db:
        job = db.get(DirectorJob, UUID(identifier))
        assert job.status == "stale" and job.result_snapshot["title"] == "A new director plan"
        assert job.usage["total_tokens"] == 70


def test_new_reference_after_paid_snapshot_cannot_be_used_by_result(director_studio):
    fixture = director_studio
    identifier = fixture.approve().json()["id"]

    def upload_later(_):
        reference = fixture.studio.reference()
        payload = fixture.client.plan.model_dump()
        payload["scenes"][0]["reference_asset_ids"] = [reference]
        fixture.client.plan = DirectorPlan.model_validate(payload)

    fixture.client.hook = upload_later
    fixture.worker(identifier)
    job = fixture.studio.api.client.get(fixture.url).json()[0]
    assert job["status"] == "failed" and job["error_code"] == "director_reference_outside_snapshot"
    assert fixture.studio.get()["title"] != "A new director plan"


def test_changed_reference_hash_invalidates_approved_director_before_call(director_studio):
    fixture = director_studio
    reference = fixture.studio.reference()
    identifier = fixture.approve().json()["id"]
    with fixture.studio.api.sessions() as db:
        db.get(MediaAsset, UUID(reference)).sha256 = "b" * 64
        db.commit()
    fixture.worker(identifier)
    assert fixture.client.calls == 0
    assert fixture.studio.api.client.get(fixture.url).json()[0]["status"] == "stale"


def test_invalid_business_plan_is_not_applied_or_automatically_regenerated(director_studio):
    fixture = director_studio
    value = fixture.client.plan.model_dump()
    value["scenes"][0]["duration_seconds"] = 15
    fixture.client.plan = DirectorPlan.model_validate(value)
    identifier = fixture.approve().json()["id"]
    before = fixture.studio.get()["revision"]
    fixture.worker(identifier)
    fixture.worker(identifier)
    assert fixture.client.calls == 1
    assert fixture.studio.get()["revision"] == before
    assert fixture.studio.api.client.get(fixture.url).json()[0]["status"] == "failed"


def test_stale_submitting_director_sweep_never_requeues_unknown_outcome(director_studio):
    fixture = director_studio
    identifier = fixture.approve().json()["id"]
    with fixture.studio.api.sessions() as db:
        job = db.get(DirectorJob, UUID(identifier))
        job.status = "submitting"
        job.submitted_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()
    fixture.studio.queued.clear()
    reconcile_directors(
        sessions=fixture.studio.api.sessions,
        settings=fixture.settings,
        director_factory=lambda _: fixture.client,
        dispatcher=lambda *args, **kwargs: fixture.studio.queued.append(args),
    )
    assert fixture.studio.queued == [] and fixture.client.calls == 0
    assert (
        fixture.studio.api.client.get(fixture.url).json()[0]["status"] == "reconciliation_required"
    )


@pytest.mark.parametrize("outcome", ["result", "error"])
def test_late_director_outcome_cannot_overwrite_operator_failure_or_bill(
    director_studio, login, outcome
):
    fixture = director_studio
    identifier = fixture.approve().json()["id"]
    before = fixture.studio.get()
    observed = {}

    def reconcile_while_call_is_in_flight(_request):
        with fixture.studio.api.sessions() as db:
            job = db.get(DirectorJob, UUID(identifier))
            job.submitted_at = datetime.now(UTC) - timedelta(hours=1)
            db.commit()
        reconcile_directors(
            sessions=fixture.studio.api.sessions,
            settings=fixture.settings,
            director_factory=lambda _: fixture.client,
            dispatcher=lambda *_args, **_kwargs: None,
        )
        login("admin")
        response = fixture.studio.api.client.get(
            "/api/v1/operations/reconciliations", params={"kind": "director"}
        )
        assert response.status_code == 200
        job = next(row for row in response.json()["items"] if row["id"] == identifier)
        response = fixture.studio.api.client.post(
            f"/api/v1/operations/reconciliations/director/{identifier}",
            headers=ORIGIN,
            json={
                "expected_updated_at": job["updated_at"],
                "reference": "Offline verified failure statement",
                "acknowledge_verified": True,
                "idempotency_key": str(uuid4()),
                "outcome": "verified_failed",
            },
        )
        assert response.status_code == 200, response.text
        response = fixture.studio.api.client.get("/api/v1/operations/costs")
        cost = next(row for row in response.json()["items"] if row["director_job_id"] == identifier)
        response = fixture.studio.api.client.post(
            f"/api/v1/operations/costs/{cost['id']}/confirm",
            headers=ORIGIN,
            json={
                "expected_updated_at": cost["updated_at"],
                "reference": "Offline final bill statement",
                "acknowledge_verified": True,
                "idempotency_key": str(uuid4()),
                "amount_usd": "0.031",
            },
        )
        assert response.status_code == 200, response.text
        with fixture.studio.api.sessions() as db:
            job = db.get(DirectorJob, UUID(identifier))
            attempt = db.scalar(select(DirectorAttempt))
            cost = db.scalar(select(ActualUsageCost))
            observed.update(
                job_updated_at=job.updated_at,
                attempt_status=attempt.status,
                attempt_error=attempt.error_code,
                attempt_completed_at=attempt.completed_at,
                cost_details=cost.usage_details,
                project_status=db.get(Project, UUID(fixture.studio.project_id)).status,
            )

    fixture.client.hook = reconcile_while_call_is_in_flight
    if outcome == "error":
        fixture.client.error = DirectorError(
            "late_director_error",
            ambiguous=True,
            response_id="resp_late_error",
            usage={"total_tokens": 99},
        )
    fixture.worker(identifier)
    fixture.worker(identifier)
    assert fixture.client.calls == 1
    current = fixture.studio.get()
    assert current["revision"] == before["revision"] and current["title"] == before["title"]
    with fixture.studio.api.sessions() as db:
        job = db.get(DirectorJob, UUID(identifier))
        assert job.status == "failed" and job.error_code == "operator_verified_failure"
        assert job.updated_at == observed["job_updated_at"]
        assert job.result_snapshot == {} and job.usage == {} and job.response_id is None
        attempt = db.scalar(select(DirectorAttempt))
        assert attempt.status == observed["attempt_status"] == "ambiguous"
        assert attempt.error_code == observed["attempt_error"] == "director_outcome_unknown"
        assert attempt.completed_at == observed["attempt_completed_at"]
        cost = db.scalar(select(ActualUsageCost))
        assert cost.status == "confirmed" and cost.amount_usd == Decimal("0.031")
        assert cost.usage_details == observed["cost_details"]
        project = db.get(Project, UUID(fixture.studio.project_id))
        assert project.status == observed["project_status"]
        assert project.committed_cost_usd == Decimal("0.05")


@pytest.mark.parametrize("enabled", [False, True])
def test_large_approved_backlog_cannot_hide_stale_paid_intent(director_studio, enabled):
    fixture = director_studio
    identifier = fixture.approve().json()["id"]
    old = datetime.now(UTC) - timedelta(days=2)
    with fixture.studio.api.sessions() as db:
        job = db.get(DirectorJob, UUID(identifier))
        job.status = "submitting"
        job.submitted_at = datetime.now(UTC) - timedelta(hours=1)
        for _ in range(101):
            db.add(
                DirectorJob(
                    project_id=job.project_id,
                    storyboard_id=job.storyboard_id,
                    input_id=job.input_id,
                    board_revision=job.board_revision,
                    context_hash=job.context_hash,
                    model=job.model,
                    request_snapshot=job.request_snapshot,
                    status="approved",
                    idempotency_key=str(uuid4()),
                    approval_fingerprint=job.approval_fingerprint,
                    approved_by_id=job.approved_by_id,
                    reserved_cost_usd=Decimal("0.01"),
                    approved_at=old,
                    dispatch_state="queued",
                    updated_at=old,
                )
            )
        db.commit()
    dispatched = []
    settings = fixture.settings.model_copy(update={"director_enabled": enabled})
    for _ in range(2):
        reconcile_directors(
            sessions=fixture.studio.api.sessions,
            settings=settings,
            director_factory=lambda _: fixture.client,
            dispatcher=lambda *args, **kwargs: dispatched.append(args),
        )
    with fixture.studio.api.sessions() as db:
        assert db.get(DirectorJob, UUID(identifier)).status == "reconciliation_required"
        if enabled:
            approved = db.scalars(select(DirectorJob).where(DirectorJob.status == "approved")).all()
            assert len(approved) == 101 and all(
                row.updated_at.replace(tzinfo=UTC) > old for row in approved
            )
    assert fixture.client.calls == 0
    if enabled:
        assert len(dispatched) == 101
        assert len({args[1] for args in dispatched}) == 101
        assert all(
            args[0] == "video.plan_storyboard" and args[1] != identifier for args in dispatched
        )
    else:
        assert dispatched == []
