"""Offline API -> DB -> task -> real fal adapter -> local download contract.

Every provider HTTP request is intercepted by MockTransport. The tiny ftyp file
is synthetic test data, not evidence of a generated or playable production clip.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
from app.catalog import seed_catalog
from app.models import ActualUsageCost, GenerationJob, Project
from app.providers.fal import FalProvider
from app.storage import StoredClip
from app.tasks import poll_generation, submit_generation
from pydantic import SecretStr
from sqlalchemy import select


def test_api_and_tasks_translate_two_scene_models_and_preserve_private_downloads(
    api, login, tmp_path
):
    login()
    origin = {"Origin": "http://localhost:3000"}
    settings = api.settings.model_copy(
        update={
            "paid_generation_enabled": True,
            "fal_key": SecretStr("offline-contract-fixture-not-a-live-provider-key"),
            "paid_approval_reference": "offline-contract-test-only-no-live-authorization",
            "storage_dir": tmp_path.resolve() / "test-only-clips",
        }
    )
    api.app.state.settings = settings
    posts = []
    requests = {}
    clients = []
    queued = []
    downloaded = []

    def provider_transport(request):
        assert request.url.host == "queue.fal.run", "All HTTP stays inside the mock transport"
        if request.method == "POST":
            assert request.url.path in {
                "/fal-ai/wan/v2.7/text-to-video",
                "/fal-ai/kling-video/v3/pro/text-to-video",
            }
            assert request.headers["X-Fal-No-Retry"] == "1"
            posts.append((request.url.path, json.loads(request.content)))
            identifier = str(uuid4())
            family = "/".join(request.url.path.split("/")[1:3])
            base = f"https://queue.fal.run/{family}/requests/{identifier}"
            requests[identifier] = request.url.path
            return httpx.Response(
                202,
                json={
                    "request_id": identifier,
                    "status_url": base + "/status",
                    "response_url": base,
                    "cancel_url": base + "/cancel",
                },
            )
        assert request.method == "GET", "This scenario never cancels or retries a paid POST"
        parts = request.url.path.split("/")
        identifier = parts[4]
        assert identifier in requests
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"request_id": identifier, "status": "COMPLETED"})
        assert request.url.path.endswith(identifier)
        return httpx.Response(
            200,
            json={
                "video": {
                    "url": f"https://v3.fal.media/files/test-only/{identifier}.mp4?token=private-media-sentinel",
                    "content_type": "video/mp4",
                    "duration": 5,
                },
            },
        )

    def provider_factory(configuration):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(provider_transport), trust_env=False
        )
        clients.append(client)
        return FalProvider(api_key=configuration.fal_key, client=client)

    def dispatcher(name, identifier, countdown=0):
        queued.append((name, identifier, countdown))

    async def synthetic_download(url, *, project_id, job_id, settings):
        assert url.startswith("https://v3.fal.media/files/test-only/")
        downloaded.append(url)
        # Real local_asset_path/FileResponse expects exactly <project UUID>/<job UUID>.mp4.
        key = f"{project_id}/{job_id}.mp4"
        destination = settings.storage_dir / key
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2TEST_ONLY_CONTRACT_FIXTURE"
        destination.write_bytes(data)
        return StoredClip(key, len(data), hashlib.sha256(data).hexdigest())

    api.app.state.provider_factory = provider_factory
    api.app.state.generation_dispatcher = dispatcher
    with api.sessions() as db:
        seed_catalog(db)
        db.commit()
    created = api.client.post(
        "/api/v1/projects",
        json={"name": "Two model offline contract", "budget_usd": "1"},
        headers=origin,
    )
    assert created.status_code == 201
    project_id = created.json()["id"]
    base = f"/api/v1/projects/{project_id}/generations"
    scene_jobs = []
    cases = [
        (
            {
                "model_id": "fal-ai/wan/v2.7/text-to-video",
                "prompt": "A calm ocean",
                "negative_prompt": "",
                "duration_seconds": 5,
                "mode": "text_to_video",
                "resolution": "720p",
                "aspect_ratio": "16:9",
                "audio_enabled": True,
            },
            "0.500000",
            "0.500000",
        ),
        (
            {
                "model_id": "fal-ai/kling-video/v3/pro/text-to-video",
                "prompt": "A mountain valley",
                "negative_prompt": "",
                "duration_seconds": 3,
                "mode": "text_to_video",
                "resolution": "1080p",
                "aspect_ratio": "9:16",
                "audio_enabled": False,
            },
            "0.836000",
            "0.336000",
        ),
    ]
    try:
        for request, project_cap, expected_estimate in cases:
            estimated = api.client.post(base + "/estimate", json=request, headers=origin)
            assert estimated.status_code == 201, estimated.text
            quote = estimated.json()
            assert quote["pricing_verified"] is False
            assert Decimal(quote["total_usd"]) == Decimal(expected_estimate)
            approval = {
                "estimate_id": quote["id"],
                "idempotency_key": str(uuid4()),
                "approved_cap_usd": project_cap,
                "approve_unverified_price": False,
            }
            assert api.client.post(base, json=approval, headers=origin).status_code == 409
            approval["approve_unverified_price"] = True
            approved = api.client.post(base, json=approval, headers=origin)
            assert approved.status_code == 202, approved.text
            job = approved.json()
            assert job["actual_cost_usd"] is None
            assert Decimal(job["reserved_cost_usd"]) == Decimal(expected_estimate)
            arguments = {
                "sessions": api.sessions,
                "settings": settings,
                "provider_factory": provider_factory,
                "dispatcher": dispatcher,
                "admission": lambda _: True,
            }
            submit_generation(job["id"], **arguments)
            with api.sessions() as db:
                db.get(GenerationJob, UUID(job["id"])).next_poll_at = datetime.now(UTC) - timedelta(
                    seconds=1
                )
                db.commit()
            poll_generation(job["id"], downloader=synthetic_download, **arguments)
            response = api.client.get(base + f"/{job['id']}")
            completed = response.json()
            assert completed["status"] == "succeeded", completed
            assert completed["actual_cost_usd"] is None
            assert (
                completed["download_url"]
                == f"/api/v1/assets/{completed['output_asset_id']}/download"
            )
            assert (
                "queue.fal.run" not in response.text
                and "private-media-sentinel" not in response.text
            )
            media = api.client.get(completed["download_url"])
            assert media.status_code == 200 and media.headers["content-type"] == "video/mp4"
            assert media.content[4:8] == b"ftyp" and b"TEST_ONLY_CONTRACT_FIXTURE" in media.content
            scene_jobs.append(completed)

        assert len(posts) == len(downloaded) == 2
        assert posts == [
            (
                "/fal-ai/wan/v2.7/text-to-video",
                {
                    "prompt": "A calm ocean",
                    "negative_prompt": "",
                    "enable_safety_checker": True,
                    "duration": 5,
                    "resolution": "720p",
                    "enable_prompt_expansion": False,
                    "aspect_ratio": "16:9",
                },
            ),
            (
                "/fal-ai/kling-video/v3/pro/text-to-video",
                {
                    "prompt": "A mountain valley",
                    "negative_prompt": "",
                    "enable_safety_checker": True,
                    "duration": "3",
                    "generate_audio": False,
                    "aspect_ratio": "9:16",
                },
            ),
        ]
        assert scene_jobs[0]["scene_id"] != scene_jobs[1]["scene_id"]
        assert scene_jobs[0]["project_id"] == scene_jobs[1]["project_id"] == project_id
        with api.sessions() as db:
            assert db.get(Project, UUID(project_id)).committed_cost_usd == Decimal("0.836000")
            usage = db.scalars(
                select(ActualUsageCost).where(ActualUsageCost.project_id == UUID(project_id))
            ).all()
            assert len(usage) == 2 and all(
                row.amount_usd is None and row.status == "unavailable" for row in usage
            )
        assert all(
            name in {"video.submit_generation", "video.poll_generation"} for name, _, _ in queued
        )
        login("other")
        for job in scene_jobs:
            assert api.client.get(job["download_url"]).status_code == 404
            assert api.client.get(base + f"/{job['id']}").status_code == 404
    finally:

        async def close_clients():
            for client in clients:
                await client.aclose()

        asyncio.run(close_clients())
