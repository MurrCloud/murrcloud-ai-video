"""Operator verification recovers existing IDs, never repeats paid submissions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from app.models import (
    ActualUsageCost,
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    Project,
    WorkspaceMutation,
)
from app.providers.base import ProviderError
from app.security import aware_utc
from sqlalchemy import func, select
from test_generation import ORIGIN
from test_generation import studio as studio


@pytest.fixture
def uncertain(studio, login):
    response = studio.approve()
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    with studio.api.sessions() as db:
        job = db.get(GenerationJob, identifier)
        job.status = "reconciliation_required"
        job.last_error_code = "submission_outcome_unknown"
        db.add(
            GenerationAttempt(
                job_id=job.id, attempt_number=1, operation="submit", status="ambiguous"
            )
        )
        db.commit()
    studio.queued.clear()
    login("admin")
    return studio, identifier


def job_payload(api, identifier, **values):
    with api.sessions() as db:
        updated = aware_utc(db.get(GenerationJob, identifier).updated_at).isoformat()
    return {
        "expected_updated_at": updated,
        "idempotency_key": str(uuid4()),
        "reference": "offline-provider-support-123",
        "acknowledge_verified": True,
        "outcome": "verified_failed",
        **values,
    }


def test_verified_failure_is_audited_idempotent_and_retains_reservation(uncertain):
    fixture, identifier = uncertain
    api = fixture.api
    payload = job_payload(api, identifier)
    path = f"/api/v1/operations/reconciliations/generation/{identifier}"
    for _ in range(2):
        response = api.client.post(path, json=payload, headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "failed"
    assert fixture.queued == [] and fixture.provider.submits == 0
    with api.sessions() as db:
        project = db.get(Project, UUID(fixture.project_id))
        job = db.get(GenerationJob, identifier)
        assert project.committed_cost_usd == job.reserved_cost_usd > 0
        assert project.approved_cap_usd == Decimal("1")
        assert (
            db.scalar(
                select(ActualUsageCost.status).where(
                    ActualUsageCost.generation_job_id == identifier
                )
            )
            == "unavailable"
        )
        assert (
            db.scalar(
                select(GenerationAttempt.status).where(GenerationAttempt.job_id == identifier)
            )
            == "ambiguous"
        )
        assert db.scalar(select(func.count()).select_from(WorkspaceMutation)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "operator.verified_failed")
            )
            == 1
        )
    assert (
        api.client.post(
            path, json={**payload, "reference": "changed-evidence"}, headers=ORIGIN
        ).status_code
        == 409
    )


def test_attach_verified_existing_id_dispatches_poll_when_paid_gate_is_off(uncertain):
    fixture, identifier = uncertain
    api = fixture.api
    api.app.state.settings = api.settings  # All live/paid flags are false.
    request_id = str(uuid4())
    payload = job_payload(api, identifier, outcome="attach_provider_id", provider_job_id=request_id)
    path = f"/api/v1/operations/reconciliations/generation/{identifier}"
    response = api.client.post(path, json=payload, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["provider_job_id"] == request_id
    assert fixture.queued == [("video.poll_generation", str(identifier), 0)]
    assert fixture.provider.submits == fixture.provider.polls == 0
    with api.sessions() as db:
        job = db.get(GenerationJob, identifier)
        assert job.status == "submitted" and job.operation_token is None
        assert (
            job.submission_snapshot["response_url"]
            == f"https://queue.fal.run/fal-ai/wan/requests/{request_id}"
        )
        assert "/text-to-video/" not in job.submission_snapshot["status_url"]
        assert db.get(Project, job.project_id).committed_cost_usd == job.reserved_cost_usd
    assert api.client.post(path, json=payload, headers=ORIGIN).status_code == 200
    assert len(fixture.queued) == 1


def test_reconciliation_requires_fresh_revision_and_explicit_evidence(uncertain):
    fixture, identifier = uncertain
    api = fixture.api
    path = f"/api/v1/operations/reconciliations/generation/{identifier}"
    payload = job_payload(api, identifier)
    for change in (
        {"acknowledge_verified": False},
        {"reference": ""},
        {"reference": "bad\nreference"},
        {"expected_updated_at": "2026-01-01T00:00:00"},
        {"outcome": "retry_paid"},
        {"provider_job_id": "https://127.0.0.1/metadata", "outcome": "attach_provider_id"},
    ):
        assert api.client.post(path, json={**payload, **change}, headers=ORIGIN).status_code == 422
    stale = {**payload, "expected_updated_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    assert api.client.post(path, json=stale, headers=ORIGIN).status_code == 409
    assert fixture.queued == []


def test_provider_reconciliation_rejects_unverified_endpoint_and_id(uncertain):
    from dataclasses import replace

    from app.generation import restore_model

    fixture, identifier = uncertain
    with fixture.api.sessions() as db:
        spec = restore_model(db.get(GenerationJob, identifier).model_snapshot)
    with pytest.raises(ProviderError):
        fixture.provider.reconcile_submission(spec, "https://metadata.internal/")
    with pytest.raises(ProviderError):
        fixture.provider.reconcile_submission(
            replace(spec, endpoint_id="made-up/video"), str(uuid4())
        )
    assert fixture.provider.submits == 0


def cost_payload(api, identifier, **values):
    with api.sessions() as db:
        updated = aware_utc(db.get(ActualUsageCost, identifier).updated_at).isoformat()
    return {
        "expected_updated_at": updated,
        "idempotency_key": str(uuid4()),
        "reference": "offline-billing-statement-456",
        "acknowledge_verified": True,
        "amount_usd": "0",
        **values,
    }


def test_charge_confirmation_blocks_uncertain_job_then_records_verified_zero(uncertain):
    fixture, job_id = uncertain
    api = fixture.api
    costs = api.client.get("/api/v1/operations/costs").json()
    identifier = UUID(costs["items"][0]["id"])
    path = f"/api/v1/operations/costs/{identifier}/confirm"
    payload = cost_payload(api, identifier, provider_charge_id="offline-charge-456")
    response = api.client.post(path, json=payload, headers=ORIGIN)
    assert (
        response.status_code == 409
        and response.json()["detail"] == "reconcile_job_before_confirming_charge"
    )
    assert (
        api.client.post(
            f"/api/v1/operations/reconciliations/generation/{job_id}",
            json=job_payload(api, job_id),
            headers=ORIGIN,
        ).status_code
        == 200
    )
    for _ in range(2):
        response = api.client.post(path, json=payload, headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert Decimal(response.json()["amount_usd"]) == 0
        assert response.json()["status"] == "confirmed"
    assert api.client.get("/api/v1/operations/costs").json()["total"] == 0
    report = api.client.get("/api/v1/reports/usage").json()["totals"]
    assert report["unknown_cost_count"] == 0 and Decimal(report["confirmed_usd"]) == 0
    with api.sessions() as db:
        project = db.get(Project, UUID(fixture.project_id))
        assert project.committed_cost_usd > 0 and project.approved_cap_usd == Decimal("1")
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "operator.charge_confirmed")
            )
            == 1
        )
    assert (
        api.client.post(path, json={**payload, "amount_usd": "99"}, headers=ORIGIN).status_code
        == 409
    )
    newer = cost_payload(api, identifier, amount_usd="99")
    assert api.client.post(path, json=newer, headers=ORIGIN).status_code == 409
    assert fixture.provider.submits == 0 and fixture.queued == []


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity", "1000001", "0.0000001"])
def test_charge_schema_rejects_invalid_amounts(uncertain, amount):
    fixture, _job_id = uncertain
    identifier = UUID(fixture.api.client.get("/api/v1/operations/costs").json()["items"][0]["id"])
    response = fixture.api.client.post(
        f"/api/v1/operations/costs/{identifier}/confirm",
        json=cost_payload(fixture.api, identifier, amount_usd=amount),
        headers=ORIGIN,
    )
    assert response.status_code == 422


def test_verified_bill_overrun_blocks_new_spend_without_raising_approval(uncertain, login):
    fixture, job_id = uncertain
    api = fixture.api
    identifier = UUID(api.client.get("/api/v1/operations/costs").json()["items"][0]["id"])
    assert (
        api.client.post(
            f"/api/v1/operations/reconciliations/generation/{job_id}",
            json=job_payload(api, job_id),
            headers=ORIGIN,
        ).status_code
        == 200
    )
    assert (
        api.client.post(
            f"/api/v1/operations/costs/{identifier}/confirm",
            json=cost_payload(api, identifier, amount_usd="3"),
            headers=ORIGIN,
        ).status_code
        == 200
    )
    report = api.client.get("/api/v1/reports/usage").json()["totals"]
    assert Decimal(report["committed_usd"]) == 1
    assert Decimal(report["exposure_usd"]) == 3
    assert Decimal(report["remaining_approved_usd"]) == -2
    assert Decimal(report["remaining_budget_usd"]) == 2
    login()
    estimate = fixture.estimate()
    assert fixture.approve(estimate, cap="1").status_code == 409
    assert fixture.approve(estimate, cap="3").status_code == 409
    accepted = fixture.approve(estimate, cap="5")
    assert accepted.status_code == 202, accepted.text
    assert Decimal(accepted.json()["reserved_cost_usd"]) == 2
    assert fixture.provider.submits == 0
    with api.sessions() as db:
        project = db.get(Project, UUID(fixture.project_id))
        assert project.approved_cap_usd == 5 and project.committed_cost_usd == 3


def test_confirmed_missing_amount_stays_unknown_and_can_be_verified(uncertain):
    fixture, job_id = uncertain
    api = fixture.api
    with api.sessions() as db:
        row = db.scalar(select(ActualUsageCost).where(ActualUsageCost.generation_job_id == job_id))
        row.status = "confirmed"
        row.amount_usd = None
        db.commit()
        identifier = row.id
    assert api.client.get("/api/v1/operations").json()["unknown_cost_count"] == 1
    assert api.client.get("/api/v1/operations/costs").json()["total"] == 1
    assert (
        api.client.post(
            f"/api/v1/operations/reconciliations/generation/{job_id}",
            json=job_payload(api, job_id),
            headers=ORIGIN,
        ).status_code
        == 200
    )
    response = api.client.post(
        f"/api/v1/operations/costs/{identifier}/confirm",
        json=cost_payload(api, identifier, amount_usd="0.20"),
        headers=ORIGIN,
    )
    assert response.status_code == 200
    assert api.client.get("/api/v1/operations").json()["unknown_cost_count"] == 0
