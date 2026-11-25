"""Offline manual-editor and whole-plan spending invariants."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app.catalog import seed_catalog
from app.models import (
    CostEstimate,
    GenerationJob,
    MediaAsset,
    Project,
    ProjectInput,
    Scene,
    SceneReferenceAsset,
    SceneVersion,
    StoryboardRevision,
)
from app.storyboard_api import router
from pydantic import SecretStr
from sqlalchemy import func, select

ORIGIN = {"Origin": "http://localhost:3000"}
WAN = "fal-ai/wan/v2.7/text-to-video"
KLING = "fal-ai/kling-video/v3/pro/text-to-video"


def scene(index=1, **changes):
    value = {
        "scene_id": f"scene_{index:03d}",
        "order": index,
        "duration_seconds": 5,
        "generation_mode": "text_to_video",
        "video_model_id": WAN,
        "resolution": "720p",
        "aspect_ratio": "16:9",
        "audio_enabled": True,
        "visual_prompt": "A calm sea",
        "negative_prompt": "",
        "narration": "A short narration",
        "reference_asset_ids": [],
        "transition": "fade",
        "status": "draft",
    }
    value.update(changes)
    return value


@pytest.fixture
def board_studio(api, login):
    if "/api/v1/projects/{project_id}/storyboard" not in api.app.openapi()["paths"]:
        api.app.include_router(router)
    login()
    settings = api.settings.model_copy(
        update={
            "paid_generation_enabled": True,
            "fal_key": SecretStr("offline-only-fal-key"),
            "paid_approval_reference": "offline-test-not-live-approval",
        }
    )
    api.app.state.settings = settings
    queued = []
    api.app.state.generation_dispatcher = lambda *args, **kwargs: queued.append((args, kwargs))
    api.app.state.director_dispatcher = lambda *args, **kwargs: queued.append((args, kwargs))
    with api.sessions() as db:
        seed_catalog(db)
        db.commit()
    created = api.client.post(
        "/api/v1/projects",
        json={"name": "Planning fixture", "budget_usd": "10", "target_duration_seconds": 10},
        headers=ORIGIN,
    )
    assert created.status_code == 201
    project_id = created.json()["id"]
    base = f"/api/v1/projects/{project_id}"

    def get():
        response = api.client.get(base + "/storyboard")
        assert response.status_code == 200
        return response.json()

    def put(scenes=None, **changes):
        current = get()
        payload = {
            key: current[key]
            for key in (
                "title",
                "target_duration_seconds",
                "language",
                "visual_style",
                "target_audience",
                "default_model_id",
                "scenes",
            )
        }
        payload.update(expected_revision=current["revision"], default_model_id=WAN)
        if scenes is not None:
            identities = {scene["scene_id"]: scene["id"] for scene in current["scenes"]}
            payload["scenes"] = [
                (
                    {**scene, "id": identities[scene["scene_id"]]}
                    if not scene.get("id") and scene["scene_id"] in identities
                    else scene
                )
                for scene in scenes
            ]
        payload.update(changes)
        return api.client.put(base + "/storyboard", json=payload, headers=ORIGIN)

    def input_text(text="A script about calm water", **changes):
        return api.client.post(
            base + "/inputs", json={"kind": "script", "text": text, **changes}, headers=ORIGIN
        )

    def estimate(**changes):
        response = api.client.post(
            base + "/storyboard/estimate",
            json={
                "expected_revision": get()["revision"],
                "retry_allowance_percent": "0",
                **changes,
            },
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text
        return response.json()

    def approve(estimate, cap="2", key=None, ack=True):
        return api.client.post(
            base + "/storyboard/approve",
            json={
                "estimate_id": estimate["id"],
                "expected_revision": estimate["revision"],
                "idempotency_key": key or str(uuid4()),
                "approved_cap_usd": cap,
                "approve_unverified_price": ack,
            },
            headers=ORIGIN,
        )

    def reference():
        with api.sessions() as db:
            identifier = uuid4()
            db.add(
                MediaAsset(
                    id=identifier,
                    project_id=UUID(project_id),
                    uploaded_by_id=api.user_ids["editor"],
                    kind="reference_image",
                    object_key=f"{project_id}/references/{identifier}.jpg",
                    mime_type="image/jpeg",
                    size_bytes=128,
                    sha256="a" * 64,
                    status="ready",
                    metadata_json={"width": 1024, "height": 768, "caption": "A planning image"},
                )
            )
            db.commit()
            return str(identifier)

    yield SimpleNamespace(
        api=api,
        settings=settings,
        queued=queued,
        project_id=project_id,
        base=base,
        get=get,
        put=put,
        input=input_text,
        estimate=estimate,
        approve=approve,
        reference=reference,
    )


def test_manual_planning_works_without_paid_keys_and_versions_inputs(board_studio):
    studio = board_studio
    studio.api.app.state.settings = studio.settings.model_copy(
        update={
            "paid_generation_enabled": False,
            "director_enabled": False,
            "fal_key": SecretStr(""),
            "openai_key": SecretStr(""),
        }
    )
    assert studio.get()["revision"] == 0
    first = studio.input().json()
    assert first["storyboard_revision"] == 1
    second = studio.input("Second script version").json()
    assert second["storyboard_revision"] == 2
    saved = studio.put([scene(1), scene(2)]).json()
    assert saved["revision"] == 3 and saved["input_id"] == second["id"]
    with studio.api.sessions() as db:
        assert db.get(ProjectInput, UUID(first["id"])).text == "A script about calm water"
        assert db.scalar(select(func.count()).select_from(StoryboardRevision)) == 3
    assert studio.queued == []


def test_reorder_and_archive_preserve_ids_versions_and_reference_history(board_studio):
    studio = board_studio
    reference = studio.reference()
    first = studio.put([scene(1, reference_asset_ids=[reference]), scene(2)]).json()
    originals = first["scenes"]
    reordered = [
        {**originals[1], "order": 1},
        {**originals[0], "order": 2, "visual_prompt": "A stormy sea"},
    ]
    saved = studio.put(reordered)
    assert saved.status_code == 200, saved.text
    assert [row["id"] for row in saved.json()["scenes"]] == [originals[1]["id"], originals[0]["id"]]
    reduced = studio.put([{**saved.json()["scenes"][0], "order": 1}])
    assert reduced.status_code == 200
    with studio.api.sessions() as db:
        removed = db.get(Scene, UUID(originals[0]["id"]))
        assert removed.archived and removed.order_index == 2
        versions = db.scalars(
            select(SceneVersion)
            .where(SceneVersion.scene_id == removed.id)
            .order_by(SceneVersion.version_number)
        ).all()
        assert versions[0].visual_prompt == "A calm sea"
        assert versions[-1].visual_prompt == "A stormy sea"
        assert db.scalar(
            select(SceneReferenceAsset).where(
                SceneReferenceAsset.scene_version_id == versions[0].id
            )
        ).asset_id == UUID(reference)


def test_optimistic_revision_conflict_keeps_saved_plan(board_studio):
    first = board_studio.put([scene(1), scene(2)]).json()
    response = board_studio.put(first["scenes"], expected_revision=0, title="Lost stale edit")
    assert response.status_code == 409 and response.json()["detail"] == "revision_conflict"
    assert board_studio.get()["title"] != "Lost stale edit"


def test_incomplete_drafts_save_but_fail_validation(board_studio):
    studio = board_studio
    assert (
        studio.put(
            [scene(1, visual_prompt=""), scene(2, generation_mode="image_to_video")]
        ).status_code
        == 200
    )
    validated = studio.api.client.post(
        studio.base + "/storyboard/validate",
        json={"expected_revision": studio.get()["revision"]},
        headers=ORIGIN,
    )
    assert validated.status_code == 200
    codes = {issue["code"] for issue in validated.json()["issues"]}
    assert codes == {"empty_prompt", "image_to_video_not_enabled"}


def test_duration_sum_uses_explicit_tolerance_and_model_bounds(board_studio):
    studio = board_studio
    studio.put([scene(1), scene(2)], target_duration_seconds=12)
    result = studio.api.client.post(
        studio.base + "/storyboard/validate",
        json={"expected_revision": studio.get()["revision"]},
        headers=ORIGIN,
    ).json()
    assert (
        not result["valid"]
        and result["total_duration_seconds"] == 10
        and result["tolerance_seconds"] == 1
    )
    studio.put(
        [scene(1, duration_seconds=1), scene(2, duration_seconds=9)], target_duration_seconds=10
    )
    result = studio.api.client.post(
        studio.base + "/storyboard/validate",
        json={"expected_revision": studio.get()["revision"]},
        headers=ORIGIN,
    ).json()
    assert "unsupported_duration" in {issue["code"] for issue in result["issues"]}


def test_foreign_reference_is_rejected_before_editor_mutation(board_studio):
    studio = board_studio
    with studio.api.sessions() as db:
        project = Project(owner_id=studio.api.user_ids["other"], name="Other owner")
        db.add(project)
        db.flush()
        identifier = uuid4()
        db.add(
            MediaAsset(
                id=identifier,
                project_id=project.id,
                kind="reference_image",
                object_key=f"{project.id}/references/{identifier}.jpg",
                mime_type="image/jpeg",
                size_bytes=128,
                status="ready",
            )
        )
        db.commit()
    response = studio.put([scene(1, reference_asset_ids=[str(identifier)])])
    assert response.status_code == 404
    assert studio.get()["revision"] == 0


def test_whole_plan_estimate_and_approval_are_atomic_and_idempotent(board_studio):
    studio = board_studio
    reference = studio.reference()
    studio.put(
        [
            scene(1, reference_asset_ids=[reference]),
            scene(2, video_model_id=KLING, resolution="1080p", audio_enabled=False),
        ]
    )
    estimated = studio.estimate()
    assert Decimal(estimated["total_usd"]) == Decimal("1.06")
    assert len(estimated["scenes"]) == 2 and not estimated["pricing_verified"]
    assert studio.approve(estimated, cap="1").status_code == 409
    assert studio.approve(estimated, ack=False).status_code == 409
    key = str(uuid4())
    response = studio.approve(estimated, key=key)
    assert response.status_code == 202, response.text
    result = response.json()
    assert studio.get()["revision"] == estimated["revision"]
    assert len(result["jobs"]) == 2 and Decimal(result["committed_cost_usd"]) == 2
    assert sum(Decimal(job["reserved_cost_usd"]) for job in result["jobs"]) == 2
    assert all(job["actual_cost_usd"] is None for job in result["jobs"])
    assert studio.approve(estimated, key=key).status_code == 202
    assert studio.approve(estimated).status_code == 409
    with studio.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 2
        jobs = db.scalars(select(GenerationJob)).all()
        assert all(job.request_snapshot.get("reference_image_url") is None for job in jobs)
        reference_version = next(job.scene_version_id for job in jobs if job.model_id == WAN)
        assert db.scalar(
            select(SceneReferenceAsset).where(
                SceneReferenceAsset.scene_version_id == reference_version
            )
        ).asset_id == UUID(reference)
    assert studio.put(studio.get()["scenes"]).status_code == 409
    assert studio.input("Cannot replace approved input").status_code == 409


def test_late_child_quote_failure_rolls_back_all_jobs_and_budget(board_studio):
    studio = board_studio
    studio.put([scene(1), scene(2)])
    estimated = studio.estimate()
    with studio.api.sessions() as db:
        db.get(CostEstimate, UUID(estimated["scenes"][1]["estimate_id"])).plan_hash = "corrupted"
        db.commit()
    response = studio.approve(estimated)
    assert response.status_code == 409
    with studio.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.get(Project, UUID(studio.project_id)).committed_cost_usd == 0
        first = db.get(CostEstimate, UUID(estimated["scenes"][0]["estimate_id"]))
        assert first.status == "pending"
    assert studio.queued == []


def test_changed_aggregate_quote_cannot_be_approved(board_studio):
    from app.models import StoryboardEstimate

    studio = board_studio
    studio.put([scene(1), scene(2)])
    estimate = studio.estimate()
    with studio.api.sessions() as db:
        db.get(StoryboardEstimate, UUID(estimate["id"])).total_usd = Decimal("0")
        db.commit()
    response = studio.approve(estimate)
    assert response.status_code == 409 and response.json()["detail"] == "estimate_changed"


def test_edit_invalidates_old_whole_plan_quote(board_studio):
    studio = board_studio
    studio.put([scene(1), scene(2)])
    estimated = studio.estimate()
    studio.put(studio.get()["scenes"], title="New plan revision")
    assert studio.approve(estimated).status_code == 409


def test_default_paid_gate_blocks_plan_reservation(board_studio):
    studio = board_studio
    studio.put([scene(1), scene(2)])
    estimated = studio.estimate()
    studio.api.app.state.settings = studio.settings.model_copy(
        update={"paid_generation_enabled": False}
    )
    assert studio.approve(estimated).status_code == 409
    with studio.api.sessions() as db:
        assert db.get(Project, UUID(studio.project_id)).committed_cost_usd == 0
    assert studio.queued == []


def test_owner_scope_and_viewer_editor_role(board_studio, login):
    studio = board_studio
    saved = studio.put([scene(1), scene(2)]).json()
    login("other")
    assert studio.api.client.get(studio.base + "/storyboard").status_code == 404
    assert studio.api.client.get(studio.base + "/inputs").status_code == 404
    login("viewer")
    payload = {
        key: saved[key]
        for key in (
            "title",
            "target_duration_seconds",
            "language",
            "visual_style",
            "target_audience",
            "default_model_id",
            "scenes",
        )
    }
    payload["expected_revision"] = saved["revision"]
    assert (
        studio.api.client.put(studio.base + "/storyboard", json=payload, headers=ORIGIN).status_code
        == 403
    )


def test_full_board_text_bound_prevents_oversized_saved_output(board_studio):
    response = board_studio.put([scene(index, narration="x" * 5000) for index in range(1, 22)])
    assert response.status_code == 422 and response.json()["detail"] == "storyboard_text_limit"


def test_input_history_returns_only_current_full_script_and_bounded_metadata(board_studio):
    studio = board_studio
    with studio.api.sessions() as db:
        for _ in range(25):
            db.add(
                ProjectInput(
                    project_id=UUID(studio.project_id),
                    created_by_id=studio.api.user_ids["editor"],
                    kind="script",
                    text="x" * 100000,
                    sha256="a" * 64,
                    warnings=[],
                )
            )
        db.commit()
    current = studio.input("Current script only").json()
    rows = studio.api.client.get(studio.base + "/inputs").json()
    assert len(rows) <= 21
    assert sum(len(row["text"]) for row in rows) == len("Current script only")
    assert next(row for row in rows if row["id"] == current["id"])["text"] == "Current script only"
    assert all(not row["text"] for row in rows if row["id"] != current["id"])


@pytest.mark.parametrize("new_scene", [False, True])
def test_standalone_clip_approval_advances_board_and_rejects_stale_editor_after_terminal(
    board_studio, new_scene
):
    studio = board_studio
    first = studio.put([scene(1), scene(2)]).json()
    if new_scene:
        # Keep an archive so the standalone append also exercises the shared
        # unique order index and the editor's active-first ordering invariant.
        studio.put([{**first["scenes"][0], "order": 1}])
    stale = studio.get()
    request = {
        "model_id": WAN,
        "prompt": "Changed by standalone clip approval",
        "negative_prompt": "",
        "duration_seconds": 5,
        "mode": "text_to_video",
        "resolution": "1080p",
        "aspect_ratio": "9:16",
        "audio_enabled": True,
    }
    if not new_scene:
        request["scene_id"] = stale["scenes"][0]["id"]
    quoted = studio.api.client.post(
        studio.base + "/generations/estimate", json=request, headers=ORIGIN
    )
    assert quoted.status_code == 201, quoted.text
    payload = {
        "estimate_id": quoted.json()["id"],
        "idempotency_key": str(uuid4()),
        "approved_cap_usd": "1",
        "approve_unverified_price": True,
    }
    approved = studio.api.client.post(studio.base + "/generations", json=payload, headers=ORIGIN)
    assert approved.status_code == 202, approved.text
    current = studio.get()
    assert current["revision"] == stale["revision"] + 1
    changed = next(row for row in current["scenes"] if row["id"] == approved.json()["scene_id"])
    assert changed["visual_prompt"] == request["prompt"] and changed["resolution"] == "1080p"
    assert changed["aspect_ratio"] == "9:16"
    assert [row["order"] for row in current["scenes"]] == list(range(1, len(current["scenes"]) + 1))
    # Repeated approval cannot consume another revision or reserve again.
    assert (
        studio.api.client.post(
            studio.base + "/generations", json=payload, headers=ORIGIN
        ).status_code
        == 202
    )
    assert studio.get()["revision"] == current["revision"]
    with studio.api.sessions() as db:
        # No provider call is made: this offline fixture marks the job terminal
        # to demonstrate that the permanent optimistic-revision guard remains.
        db.get(GenerationJob, UUID(approved.json()["id"])).status = "cancelled"
        db.commit()
        history = db.scalar(
            select(StoryboardRevision).where(StoryboardRevision.revision == current["revision"])
        )
        assert (
            next(row for row in history.snapshot["scenes"] if row["id"] == changed["id"])[
                "visual_prompt"
            ]
            == request["prompt"]
        )
        if new_scene:
            archived = db.get(Scene, UUID(first["scenes"][1]["id"]))
            assert archived.archived and archived.order_index == len(current["scenes"]) + 1
    old_payload = {
        key: stale[key]
        for key in (
            "title",
            "target_duration_seconds",
            "language",
            "visual_style",
            "target_audience",
            "default_model_id",
            "scenes",
        )
    }
    old_payload["expected_revision"] = stale["revision"]
    response = studio.api.client.put(studio.base + "/storyboard", json=old_payload, headers=ORIGIN)
    assert response.status_code == 409 and response.json()["detail"] == "revision_conflict"
    assert (
        next(row for row in studio.get()["scenes"] if row["id"] == changed["id"])["visual_prompt"]
        == request["prompt"]
    )
