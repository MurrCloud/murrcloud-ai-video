"""Offline paid-boundary, interruption and owner-scope tests. No provider I/O."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from app.catalog import seed_catalog
from app.generation import request_cancel
from app.models import (
    ActualUsageCost,
    CostEstimate,
    GenerationAttempt,
    GenerationJob,
    MediaAsset,
    Project,
    ProjectSettings,
    SceneVersion,
    User,
)
from app.providers.base import (
    CancelOutcome,
    CostQuote,
    GenerationResult,
    GenerationStatus,
    ProviderError,
    Submission,
)
from app.providers.fal import FalProvider
from app.storage import StoredClip
from app.tasks import cancel_generation, poll_generation, reconcile_pending, submit_generation
from pydantic import SecretStr
from sqlalchemy import func, select

ORIGIN = {"Origin": "http://localhost:3000"}
MODEL = "fal-ai/wan/v2.7/text-to-video"
REQUEST = {
    "model_id": MODEL,
    "prompt": "A calm sea at sunrise",
    "negative_prompt": "",
    "duration_seconds": 5,
    "mode": "text_to_video",
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "audio_enabled": True,
}


class OfflineProvider(FalProvider):
    """Uses the real local adapter validation; every transport is explicitly fake."""

    def __init__(self):
        def no_network(_):
            raise AssertionError("These tests must never access a provider")

        self.test_client = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
        super().__init__(api_key="offline-only-not-a-live-key", client=self.test_client)
        self.submits = 0
        self.polls = 0
        self.cancels = 0
        self.on_submit = None
        self.on_poll = None
        self.submit_error = None
        self.poll_error = None
        self.cancel_confirmed = False
        self.state = "completed"
        self.unknown_price = False
        self.verified = False

    async def estimate_cost(self, model, request):
        await self.validate_request(model, request)
        return CostQuote(
            None if self.unknown_price else Decimal("0.50"),
            "unavailable" if self.unknown_price else "public_model_page",
            self.verified,
            {"test": "offline"},
        )

    async def submit_generation(self, model, request, *, paid_authorized=False):
        assert paid_authorized
        self.submits += 1
        if self.on_submit:
            self.on_submit()
        if self.submit_error:
            raise self.submit_error
        identifier = str(uuid4())
        base = f"https://queue.fal.run/fal-ai/wan/requests/{identifier}"
        return Submission(
            request_id=identifier,
            status_url=base + "/status",
            response_url=base,
            cancel_url=base + "/cancel",
        )

    async def get_generation_status(self, model, submission):
        self.polls += 1
        if self.on_poll:
            self.on_poll()
        if self.poll_error:
            raise self.poll_error
        return GenerationStatus(self.state)

    async def retrieve_result(self, model, submission):
        return GenerationResult("https://v3.fal.media/files/offline-fixture.mp4", {"fps": 24})

    async def cancel_generation(self, model, submission):
        self.cancels += 1
        return CancelOutcome(
            "cancelled" if self.cancel_confirmed else "requested", self.cancel_confirmed
        )

    async def aclose(self):
        # One explicit shared mock is reused across API and worker calls.
        pass


@pytest.fixture
def studio(api, login):
    login()
    provider = OfflineProvider()
    settings = api.settings.model_copy(
        update={
            "paid_generation_enabled": True,
            "fal_key": SecretStr("offline-only-not-a-live-key"),
            "paid_approval_reference": "offline-test-no-live-authorization",
            "generation_poll_base_seconds": 1,
            "generation_poll_max_seconds": 8,
            "generation_poll_max_attempts": 4,
        }
    )
    api.app.state.settings = settings
    api.app.state.provider_factory = lambda _: provider
    queued = []

    def dispatch(name, identifier, countdown=0):
        queued.append((name, identifier, countdown))

    api.app.state.generation_dispatcher = dispatch
    with api.sessions() as db:
        seed_catalog(db)
        db.commit()
    response = api.client.post(
        "/api/v1/projects", json={"name": "Offline clip test", "budget_usd": "5"}, headers=ORIGIN
    )
    assert response.status_code == 201
    project_id = response.json()["id"]
    url = f"/api/v1/projects/{project_id}/generations"

    def estimate(**overrides):
        response = api.client.post(url + "/estimate", json={**REQUEST, **overrides}, headers=ORIGIN)
        assert response.status_code == 201, response.text
        return response.json()

    def approve(estimate_value=None, *, cap="1", key=None, ack=True):
        estimate_value = estimate_value or estimate()
        response = api.client.post(
            url,
            json={
                "estimate_id": estimate_value["id"],
                "idempotency_key": key or str(uuid4()),
                "approved_cap_usd": cap,
                "approve_unverified_price": ack,
            },
            headers=ORIGIN,
        )
        return response

    async def download(_, *, project_id, job_id, settings):
        return StoredClip(f"projects/{project_id}/jobs/{job_id}/clip.mp4", 4096, "a" * 64)

    def worker(function, identifier, **overrides):
        arguments = dict(
            sessions=api.sessions,
            settings=settings,
            provider_factory=lambda _: provider,
            dispatcher=dispatch,
            admission=lambda _: True,
        )
        if function is poll_generation:
            arguments["downloader"] = download
        arguments.update(overrides)
        return function(identifier, **arguments)

    def due(identifier):
        with api.sessions() as db:
            job = db.get(GenerationJob, UUID(str(identifier)))
            job.next_poll_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()

    yield SimpleNamespace(
        api=api,
        provider=provider,
        settings=settings,
        queued=queued,
        project_id=project_id,
        url=url,
        estimate=estimate,
        approve=approve,
        worker=worker,
        due=due,
        download=download,
    )
    asyncio.run(provider.test_client.aclose())


def test_public_quote_requires_ack_and_reserves_cap_not_actual(studio):
    estimate = studio.estimate(retry_allowance_percent="20")
    assert Decimal(estimate["base_usd"]) == Decimal("0.5")
    assert Decimal(estimate["total_usd"]) == Decimal("0.6")
    assert estimate["pricing_verified"] is False
    assert studio.approve(estimate, ack=False).status_code == 409
    response = studio.approve(estimate, cap="1")
    assert response.status_code == 202, response.text
    job = response.json()
    assert Decimal(job["reserved_cost_usd"]) == Decimal("1")
    assert job["actual_cost_usd"] is None
    assert job["actual_cost_status"] == "unavailable"
    assert studio.provider.submits == 0
    with studio.api.sessions() as db:
        project = db.get(Project, UUID(studio.project_id))
        assert project.committed_cost_usd == Decimal("1")
        assert db.scalar(select(ActualUsageCost)).amount_usd is None


def test_unknown_price_is_not_zero_and_needs_explicit_cap_approval(studio):
    studio.provider.unknown_price = True
    estimate = studio.estimate()
    assert estimate["base_usd"] is None and estimate["total_usd"] is None
    assert studio.approve(estimate, ack=False).status_code == 409
    response = studio.approve(estimate, cap="2")
    assert response.status_code == 202
    assert response.json()["estimated_cost_usd"] is None
    assert Decimal(response.json()["reserved_cost_usd"]) == Decimal("2")


def test_disabled_global_gate_reserves_nothing_and_queues_nothing(studio):
    estimate = studio.estimate()
    studio.api.app.state.settings = studio.settings.model_copy(
        update={"paid_generation_enabled": False}
    )
    assert studio.approve(estimate).status_code == 409
    assert not studio.queued
    with studio.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.get(Project, UUID(studio.project_id)).committed_cost_usd == 0


def test_idempotency_and_single_use_estimate_block_double_reservation(studio):
    estimate = studio.estimate()
    key = str(uuid4())
    first = studio.approve(estimate, key=key)
    second = studio.approve(estimate, key=key)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert studio.approve(estimate).status_code == 409
    assert studio.approve(estimate, key=key, cap="2").status_code == 409
    with studio.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1
        assert db.get(Project, UUID(studio.project_id)).committed_cost_usd == Decimal("1")


@pytest.mark.parametrize("change", ["expire", "settings", "model"])
def test_expired_or_changed_approval_inputs_are_rejected(studio, change):
    estimate = studio.estimate()
    with studio.api.sessions() as db:
        if change == "expire":
            db.get(CostEstimate, UUID(estimate["id"])).expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
        elif change == "settings":
            db.scalar(
                select(ProjectSettings).where(ProjectSettings.project_id == UUID(studio.project_id))
            ).visual_style = "changed"
        else:
            from app.models import VideoModel

            db.scalar(select(VideoModel).where(VideoModel.model_id == MODEL)).price_usd = Decimal(
                "9"
            )
        db.commit()
    assert studio.approve(estimate).status_code == 409


def test_phase2_rejects_reference_urls_and_unsupported_mode(studio):
    for update in (
        {"reference_image_url": "https://example.com/private.jpg"},
        {"mode": "image_to_video"},
    ):
        response = studio.api.client.post(
            studio.url + "/estimate", json={**REQUEST, **update}, headers=ORIGIN
        )
        assert response.status_code == 422


def test_other_owner_and_viewer_cannot_mutate_generation(studio, login):
    estimate = studio.estimate()
    login("other")
    assert studio.api.client.get(studio.url).status_code == 404
    assert studio.approve(estimate).status_code == 404
    login("viewer")
    assert (
        studio.api.client.post(studio.url + "/estimate", json=REQUEST, headers=ORIGIN).status_code
        == 403
    )
    assert studio.approve(estimate).status_code == 403


def test_broker_failure_keeps_durable_job_and_safe_dispatch_retry(studio):
    def unavailable(*_, **__):
        raise ConnectionError("never expose broker-password-sentinel")

    studio.api.app.state.generation_dispatcher = unavailable
    response = studio.approve()
    assert response.status_code == 202
    job = response.json()
    assert job["dispatch_state"] == "error" and job["dispatch_error_code"] == "broker_unavailable"
    assert "sentinel" not in response.text
    studio.api.app.state.generation_dispatcher = lambda *_, **__: None
    retried = studio.api.client.post(studio.url + f"/{job['id']}/dispatch", headers=ORIGIN)
    assert retried.status_code == 202 and retried.json()["dispatch_state"] == "queued"
    assert studio.provider.submits == 0


def test_paid_intent_is_committed_before_post_and_redelivery_only_polls(studio):
    job = studio.approve().json()

    def durable_intent():
        with studio.api.sessions() as db:
            assert db.get(GenerationJob, UUID(job["id"])).status == "submitting"
            assert db.scalar(select(GenerationAttempt)).status == "started"

    studio.provider.on_submit = durable_intent
    studio.worker(submit_generation, job["id"])
    studio.worker(submit_generation, job["id"])
    assert studio.provider.submits == 1
    assert studio.queued[-1][0] == "video.poll_generation"
    detail = studio.api.client.get(studio.url + f"/{job['id']}").json()
    assert detail["status"] == "submitted"
    assert "queue.fal.run" not in str(detail)
    assert "submission_snapshot" not in detail


def test_worker_host_gate_prevents_paid_intent_and_submission(studio):
    job = studio.approve().json()
    studio.worker(submit_generation, job["id"], admission=lambda _: False)
    assert studio.provider.submits == 0
    with studio.api.sessions() as db:
        assert db.get(GenerationJob, UUID(job["id"])).status == "approved"
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 0


def test_worker_model_disable_before_submit_has_no_paid_call(studio):
    job = studio.approve().json()
    from app.models import VideoModel

    with studio.api.sessions() as db:
        db.scalar(select(VideoModel).where(VideoModel.model_id == MODEL)).enabled = False
        db.commit()
    studio.worker(submit_generation, job["id"])
    assert studio.provider.submits == 0


def test_crash_after_paid_post_never_submits_again(studio):
    class WorkerCrash(BaseException):
        pass

    job = studio.approve().json()

    def crash():
        raise WorkerCrash

    studio.provider.on_submit = crash
    with pytest.raises(WorkerCrash):
        studio.worker(submit_generation, job["id"])
    studio.provider.on_submit = None
    studio.worker(submit_generation, job["id"])
    assert studio.provider.submits == 1
    with studio.api.sessions() as db:
        current = db.get(GenerationJob, UUID(job["id"]))
        assert current.status == "reconciliation_required"
        assert current.provider_job_id is None
        assert current.reserved_cost_usd == Decimal("1")


@pytest.mark.parametrize("ambiguous", [True, False])
def test_submission_errors_never_get_automatic_paid_retries(studio, ambiguous):
    job = studio.approve().json()
    studio.provider.submit_error = ProviderError(
        "provider_timeout", ambiguous=ambiguous, retryable=True
    )
    studio.worker(submit_generation, job["id"])
    studio.worker(submit_generation, job["id"])
    assert studio.provider.submits == 1
    with studio.api.sessions() as db:
        row = db.get(GenerationJob, UUID(job["id"]))
        assert row.status == ("reconciliation_required" if ambiguous else "failed")
        assert db.get(Project, UUID(studio.project_id)).committed_cost_usd == Decimal("1")


def test_poll_backoff_is_bounded_and_never_reposts(studio):
    job = studio.approve().json()
    studio.worker(submit_generation, job["id"])
    studio.provider.poll_error = ProviderError("temporary_provider_error", retryable=True)
    for _ in range(4):
        studio.due(job["id"])
        studio.worker(poll_generation, job["id"])
    with studio.api.sessions() as db:
        current = db.get(GenerationJob, UUID(job["id"]))
        assert current.status == "reconciliation_required"
        assert current.poll_count == 4
    assert studio.provider.polls == 4 and studio.provider.submits == 1
    assert max(countdown for _, _, countdown in studio.queued) <= 8


def test_download_waits_for_host_admission_without_paid_retry(studio):
    job = studio.approve().json()
    studio.worker(submit_generation, job["id"])
    studio.due(job["id"])
    downloads = []

    async def download(*_, **__):
        downloads.append(True)
        raise AssertionError

    studio.worker(poll_generation, job["id"], admission=lambda _: False, downloader=download)
    assert not downloads and studio.provider.submits == 1
    assert (
        studio.api.client.get(studio.url + f"/{job['id']}").json()["last_error_code"]
        == "host_admission_refused"
    )


def test_success_and_regeneration_keep_previous_version_and_asset(studio):
    first = studio.approve(cap="1").json()
    studio.worker(submit_generation, first["id"])
    studio.due(first["id"])
    studio.worker(poll_generation, first["id"])
    completed = studio.api.client.get(studio.url + f"/{first['id']}").json()
    assert completed["status"] == "succeeded" and completed["download_url"]
    assert completed["actual_cost_usd"] is None
    with studio.api.sessions() as db:
        assert db.get(Project, UUID(studio.project_id)).status == "clips_ready"
    estimate = studio.estimate(scene_id=first["scene_id"], prompt="Stormy sea after sunset")
    second_response = studio.approve(estimate, cap="2")
    assert second_response.status_code == 202, second_response.text
    second = second_response.json()
    assert second["scene_id"] == first["scene_id"] and second["version_number"] == 2
    assert second["scene_version_id"] != first["scene_version_id"]
    with studio.api.sessions() as db:
        original = db.get(SceneVersion, UUID(first["scene_version_id"]))
        assert original.visual_prompt == REQUEST["prompt"]
        assert db.get(MediaAsset, UUID(completed["output_asset_id"])).status == "ready"
        assert db.get(GenerationJob, UUID(first["id"])).output_asset_id == UUID(
            completed["output_asset_id"]
        )


def test_cancel_ambiguity_keeps_reservation_and_actual_unknown(studio):
    job = studio.approve().json()
    studio.worker(submit_generation, job["id"])
    response = studio.api.client.post(studio.url + f"/{job['id']}/cancel", headers=ORIGIN)
    assert response.status_code == 202
    studio.worker(cancel_generation, job["id"])
    detail = studio.api.client.get(studio.url + f"/{job['id']}").json()
    assert detail["status"] == "cancellation_requested" and detail["no_longer_wanted"]
    assert detail["actual_cost_usd"] is None and Decimal(detail["reserved_cost_usd"]) == 1
    assert studio.queued[-1][0] == "video.poll_generation"


def test_cancel_during_submit_is_honored_after_known_id_without_refund(studio):
    job = studio.approve().json()

    def cancel_during_post():
        with studio.api.sessions() as db:
            user = db.get(User, studio.api.user_ids["editor"])
            request_cancel(db, UUID(studio.project_id), UUID(job["id"]), user)

    studio.provider.on_submit = cancel_during_post
    studio.worker(submit_generation, job["id"])
    detail = studio.api.client.get(studio.url + f"/{job['id']}").json()
    assert detail["status"] == "cancellation_requested"
    assert studio.queued[-1][0] == "video.cancel_generation"
    assert studio.provider.submits == 1


def test_pending_reconciliation_never_enqueues_unknown_submission(studio):
    job = studio.approve().json()
    with studio.api.sessions() as db:
        row = db.get(GenerationJob, UUID(job["id"]))
        row.status = "submitting"
        row.operation_started_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()
    studio.queued.clear()
    reconcile_pending(
        sessions=studio.api.sessions,
        settings=studio.settings,
        dispatcher=lambda *a, **k: studio.queued.append(a),
    )
    assert studio.queued == [] and studio.provider.submits == 0
    with studio.api.sessions() as db:
        assert db.get(GenerationJob, UUID(job["id"])).status == "reconciliation_required"


def test_stale_poll_lease_recovers_only_known_provider_get(studio):
    job = studio.approve().json()
    studio.worker(submit_generation, job["id"])
    studio.due(job["id"])
    with studio.api.sessions() as db:
        current = db.get(GenerationJob, UUID(job["id"]))
        current.operation_token = uuid4()
        current.operation_started_at = datetime.now(UTC)
        db.commit()
    studio.queued.clear()
    reconcile_pending(
        sessions=studio.api.sessions,
        settings=studio.settings,
        dispatcher=lambda *a, **k: studio.queued.append(a),
    )
    assert studio.queued == []
    with studio.api.sessions() as db:
        current = db.get(GenerationJob, UUID(job["id"]))
        current.operation_started_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()
    reconcile_pending(
        sessions=studio.api.sessions,
        settings=studio.settings,
        dispatcher=lambda *a, **k: studio.queued.append(a),
    )
    assert studio.queued == [("video.poll_generation", job["id"])]
    studio.worker(poll_generation, job["id"])
    assert studio.provider.submits == 1 and studio.provider.polls == 1
    assert studio.api.client.get(studio.url + f"/{job['id']}").json()["status"] == "succeeded"


def test_cancel_during_completed_poll_skips_download_and_does_not_claim_refund(studio):
    job = studio.approve().json()
    studio.worker(submit_generation, job["id"])

    def local_cancel():
        with studio.api.sessions() as db:
            request_cancel(
                db,
                UUID(studio.project_id),
                UUID(job["id"]),
                db.get(User, studio.api.user_ids["editor"]),
            )

    studio.provider.on_poll = local_cancel
    studio.due(job["id"])

    async def must_not_download(*_, **__):
        raise AssertionError("Cancelled local result must not be downloaded")

    studio.worker(poll_generation, job["id"], downloader=must_not_download)
    detail = studio.api.client.get(studio.url + f"/{job['id']}").json()
    assert detail["status"] == "abandoned" and detail["download_url"] is None
    assert detail["actual_cost_usd"] is None and Decimal(detail["reserved_cost_usd"]) == 1


def test_cancel_during_download_rejects_new_asset_without_publishing(studio):
    job = studio.approve().json()
    studio.worker(submit_generation, job["id"])
    studio.due(job["id"])

    async def download_then_cancel(url, **kwargs):
        with studio.api.sessions() as db:
            request_cancel(
                db,
                UUID(studio.project_id),
                UUID(job["id"]),
                db.get(User, studio.api.user_ids["editor"]),
            )
        return await studio.download(url, **kwargs)

    studio.worker(poll_generation, job["id"], downloader=download_then_cancel)
    detail = studio.api.client.get(studio.url + f"/{job['id']}").json()
    assert detail["status"] == "abandoned" and detail["output_asset_id"] is None
    with studio.api.sessions() as db:
        assert db.scalar(select(MediaAsset)).status == "rejected"
        assert db.get(Project, UUID(studio.project_id)).committed_cost_usd == 1


@pytest.mark.parametrize("stage", ["poll", "download"])
def test_actual_worker_interruption_recovers_after_lease_without_reposting(studio, stage):
    class WorkerCrash(BaseException):
        pass

    job = studio.approve().json()
    studio.worker(submit_generation, job["id"])
    studio.due(job["id"])

    def crash():
        raise WorkerCrash

    async def crash_download(*_, **__):
        raise WorkerCrash

    if stage == "poll":
        studio.provider.on_poll = crash
    with pytest.raises(WorkerCrash):
        studio.worker(
            poll_generation,
            job["id"],
            **({"downloader": crash_download} if stage == "download" else {}),
        )
    studio.provider.on_poll = None
    studio.worker(poll_generation, job["id"])
    assert studio.provider.submits == 1
    with studio.api.sessions() as db:
        current = db.get(GenerationJob, UUID(job["id"]))
        assert current.operation_token is not None
        current.operation_started_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()
    reconcile_pending(
        sessions=studio.api.sessions, settings=studio.settings, dispatcher=lambda *_, **__: None
    )
    studio.worker(poll_generation, job["id"])
    assert studio.provider.submits == 1
    assert studio.api.client.get(studio.url + f"/{job['id']}").json()["status"] == "succeeded"


def test_manual_reconciliation_backlog_cannot_starve_active_known_id(studio):
    job = studio.approve().json()
    studio.worker(submit_generation, job["id"])
    studio.due(job["id"])
    with studio.api.sessions() as db:
        original = db.get(GenerationJob, UUID(job["id"]))
        for _ in range(101):
            db.add(
                GenerationJob(
                    project_id=original.project_id,
                    scene_version_id=original.scene_version_id,
                    model_id=original.model_id,
                    provider=original.provider,
                    endpoint_id=original.endpoint_id,
                    idempotency_key=str(uuid4()),
                    status="reconciliation_required",
                    updated_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        db.commit()
    studio.queued.clear()
    reconcile_pending(
        sessions=studio.api.sessions,
        settings=studio.settings,
        dispatcher=lambda *a, **k: studio.queued.append(a),
    )
    assert studio.queued == [("video.poll_generation", job["id"])]


@pytest.mark.parametrize(
    "status",
    ["approved", "submitting", "running", "reconciliation_required", "cancellation_requested"],
)
def test_scene_regeneration_waits_for_prior_paid_outcome(studio, status):
    job = studio.approve().json()
    with studio.api.sessions() as db:
        db.get(GenerationJob, UUID(job["id"])).status = status
        db.commit()
    response = studio.api.client.post(
        studio.url + "/estimate", json={**REQUEST, "scene_id": job["scene_id"]}, headers=ORIGIN
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "scene_generation_requires_completion_or_reconciliation"
    assert studio.provider.submits == 0
