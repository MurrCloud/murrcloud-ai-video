"""Workspace reuse never clones private media, paid intent or prior spending."""

import json
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from app.catalog import seed_catalog
from app.library_api import router
from app.models import (
    ActualUsageCost,
    GenerationJob,
    MediaAsset,
    Project,
    ProjectInput,
    ProjectSettings,
    ProjectTemplate,
    RenderingJob,
    Scene,
    SceneReferenceAsset,
    SceneVersion,
    Storyboard,
    WorkspaceMutation,
)
from app.production_schemas import ProductionSettings
from sqlalchemy import func, select
from test_storyboard import scene

ORIGIN = {"Origin": "http://localhost:3000"}
WAN = "fal-ai/wan/v2.7/text-to-video"
KLING = "fal-ai/kling-video/v3/pro/text-to-video"


@pytest.fixture
def library(api, login):
    if "/api/v1/library/projects" not in api.app.openapi()["paths"]:
        api.app.include_router(router)
    login()
    with api.sessions() as db:
        seed_catalog(db)
        db.commit()
    response = api.client.post(
        "/api/v1/projects",
        headers=ORIGIN,
        json={
            "name": "Original launch",
            "prompt": "A private production brief",
            "target_duration_seconds": 10,
            "budget_usd": "10",
        },
    )
    assert response.status_code == 201
    project_id = response.json()["id"]
    base = f"/api/v1/projects/{project_id}"
    response = api.client.put(
        base + "/storyboard",
        headers=ORIGIN,
        json={
            "expected_revision": 0,
            "title": "Launch storyboard",
            "target_duration_seconds": 10,
            "language": "en",
            "visual_style": "cinematic",
            "target_audience": "Internal buyers",
            "default_model_id": WAN,
            "scenes": [
                scene(1),
                scene(2, video_model_id=KLING, resolution="1080p", audio_enabled=False),
            ],
        },
    )
    assert response.status_code == 200, response.text
    response = api.client.post(
        base + "/inputs",
        headers=ORIGIN,
        json={"kind": "script", "text": "English and 中文 source script"},
    )
    assert response.status_code == 201
    with api.sessions() as db:
        source = db.get(Project, UUID(project_id))
        source.status = "completed"
        source.approved_cap_usd, source.committed_cost_usd = Decimal("5"), Decimal("2")
        assets = []
        for kind, mime, extension in (
            ("reference_image", "image/jpeg", "jpg"),
            ("music", "audio/mpeg", "mp3"),
        ):
            identifier = uuid4()
            asset = MediaAsset(
                id=identifier,
                project_id=source.id,
                kind=kind,
                object_key=f"{project_id}/productions/{identifier}.{extension}",
                mime_type=mime,
                size_bytes=10,
                sha256="a" * 64,
                status="ready",
            )
            db.add(asset)
            assets.append(identifier)
        db.flush()
        row = db.scalar(select(ProjectSettings).where(ProjectSettings.project_id == source.id))
        production = ProductionSettings(
            music_asset_id=assets[1], watermark_asset_id=assets[0], allow_upscale=True
        )
        row.composition = {
            **row.composition,
            "production": production.model_dump(mode="json"),
            "api_key": "fixture-secret-should-not-copy",
            "storage_path": "/private/source-location",
        }
        first = db.scalar(
            select(Scene).where(Scene.project_id == source.id).order_by(Scene.order_index)
        )
        first.reference_asset_ids = [str(assets[0])]
        first.generation_settings = {
            **first.generation_settings,
            "credential": "fixture-credential",
        }
        db.commit()

    def items(**params):
        response = api.client.get("/api/v1/library/projects", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def duplicate(key=None, name="Copied launch"):
        return api.client.post(
            base + "/duplicate",
            headers=ORIGIN,
            json={"name": name, "idempotency_key": key or str(uuid4())},
        )

    def template(**changes):
        return api.client.post(
            base + "/templates",
            headers=ORIGIN,
            json={
                "name": "Reusable launch",
                "description": "A portable launch plan",
                "idempotency_key": str(uuid4()),
                **changes,
            },
        )

    return type(
        "LibraryFixture",
        (),
        {
            "api": api,
            "project_id": project_id,
            "base": base,
            "items": staticmethod(items),
            "duplicate": staticmethod(duplicate),
            "template": staticmethod(template),
        },
    )()


def test_duplicate_is_atomic_idempotent_and_resets_paid_and_media_state(library):
    p = library
    key = str(uuid4())
    response = p.duplicate(key)
    assert response.status_code == 201, response.text
    copied_id = UUID(response.json()["id"])
    assert copied_id != UUID(p.project_id)
    assert p.duplicate(key).json()["id"] == str(copied_id)
    assert p.duplicate(key, name="Different payload").status_code == 409
    with p.api.sessions() as db:
        project = db.get(Project, copied_id)
        assert project.status == "draft" and not project.archived
        assert (
            project.budget_usd == 10
            and project.committed_cost_usd == 0
            and project.approved_cap_usd is None
        )
        assert db.scalar(select(func.count()).select_from(WorkspaceMutation)) == 1
        assert not db.scalar(select(GenerationJob.id).where(GenerationJob.project_id == copied_id))
        assert not db.scalar(select(RenderingJob.id).where(RenderingJob.project_id == copied_id))
        assert not db.scalar(select(MediaAsset.id).where(MediaAsset.project_id == copied_id))
        scenes = db.scalars(
            select(Scene).where(Scene.project_id == copied_id).order_by(Scene.order_index)
        ).all()
        assert [row.video_model_id for row in scenes] == [WAN, KLING]
        assert all(row.reference_asset_ids == [] and row.status == "draft" for row in scenes)
        versions = db.scalars(
            select(SceneVersion).where(SceneVersion.scene_id.in_([s.id for s in scenes]))
        ).all()
        assert all(
            v.version_number == 1 and v.restored_from_id is None and v.status == "draft"
            for v in versions
        )
        assert not db.scalar(
            select(SceneReferenceAsset.id).where(
                SceneReferenceAsset.scene_version_id.in_([v.id for v in versions])
            )
        )
        settings = db.scalar(select(ProjectSettings).where(ProjectSettings.project_id == copied_id))
        production = settings.composition["production"]
        assert production["music_asset_id"] is None and production["watermark_asset_id"] is None
        assert not production["allow_upscale"]
        assert "fixture-secret" not in json.dumps(settings.composition)
        board = db.scalar(select(Storyboard).where(Storyboard.project_id == copied_id))
        source = db.get(ProjectInput, board.input_id)
        assert source.project_id == copied_id and source.text == "English and 中文 source script"
        assert source.source_name == "Copied script"
        original = db.get(Project, UUID(p.project_id))
        assert original.committed_cost_usd == 2 and original.status == "completed"


def test_library_pagination_filter_wildcards_and_prompt_bounds(library, login):
    p = library
    p.duplicate(name="A name with % literal")
    p.duplicate(name="Second copy")
    first = p.items(limit=1)
    second = p.items(limit=1, offset=1)
    assert first["total"] == 3 and first["items"][0]["id"] != second["items"][0]["id"]
    assert p.items(q="%")["total"] == 1
    assert p.items(status="completed")["total"] == 1
    assert p.api.client.get("/api/v1/library/projects?status=not-a-status").status_code == 422
    assert p.api.client.get("/api/v1/library/projects?limit=101").status_code == 422
    with p.api.sessions() as db:
        db.get(Project, UUID(p.project_id)).prompt = "中" * 50000
        db.commit()
    row = next(row for row in p.items()["items"] if row["id"] == p.project_id)
    assert len(row["prompt"]) == 500 and row["prompt_truncated"]
    login("other")
    assert p.items()["total"] == 0
    assert p.duplicate().status_code == 404
    login("admin")
    assert p.items()["total"] == 3


def test_metadata_and_archive_are_optimistic_and_do_not_delete_media(library):
    p = library
    item = p.items()["items"][0]
    response = p.api.client.patch(
        p.base + "/metadata",
        headers=ORIGIN,
        json={
            "expected_updated_at": item["updated_at"],
            "name": "Renamed project",
            "budget_usd": "4",
        },
    )
    assert (
        response.status_code == 409
        and response.json()["detail"] == "budget_below_existing_commitments"
    )
    response = p.api.client.patch(
        p.base + "/metadata",
        headers=ORIGIN,
        json={
            "expected_updated_at": item["updated_at"],
            "name": "Renamed project",
            "budget_usd": "12",
        },
    )
    assert response.status_code == 200
    assert Decimal(response.json()["approved_cap_usd"]) == 5
    assert (
        p.api.client.post(
            p.base + "/archive",
            headers=ORIGIN,
            json={"expected_updated_at": item["updated_at"], "archived": True},
        ).status_code
        == 409
    )
    current = p.items()["items"][0]
    response = p.api.client.post(
        p.base + "/archive",
        headers=ORIGIN,
        json={"expected_updated_at": current["updated_at"], "archived": True},
    )
    assert response.status_code == 200 and response.json()["archived"]
    assert p.items()["total"] == 0 and p.items(archived=True)["total"] == 1
    with p.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(MediaAsset)) == 2
    assert p.duplicate().status_code == 201
    archived = p.items(archived=True)["items"][0]
    assert (
        p.api.client.post(
            p.base + "/archive",
            headers=ORIGIN,
            json={"expected_updated_at": archived["updated_at"], "archived": False},
        ).status_code
        == 200
    )


def test_metadata_budget_cannot_drop_below_verified_charge_exposure(library):
    p = library
    with p.api.sessions() as db:
        version = db.scalar(
            select(SceneVersion).join(Scene).where(Scene.project_id == UUID(p.project_id))
        )
        job = GenerationJob(
            project_id=UUID(p.project_id),
            scene_version_id=version.id,
            model_id=WAN,
            provider="fal",
            endpoint_id=WAN,
            idempotency_key=str(uuid4()),
            status="succeeded",
            reserved_cost_usd=Decimal("2"),
        )
        db.add(job)
        db.flush()
        db.add(
            ActualUsageCost(
                project_id=job.project_id,
                generation_job_id=job.id,
                provider="fal",
                amount_usd=Decimal("9"),
                status="confirmed",
            )
        )
        db.commit()
    payload = {
        "expected_updated_at": p.items()["items"][0]["updated_at"],
        "name": "Budget accounts for verified overrun",
        "budget_usd": "8",
    }
    response = p.api.client.patch(p.base + "/metadata", headers=ORIGIN, json=payload)
    assert response.status_code == 409
    assert response.json()["detail"] == "budget_below_existing_commitments"
    with p.api.sessions() as db:
        assert db.get(Project, UUID(p.project_id)).budget_usd == 10
    response = p.api.client.patch(
        p.base + "/metadata", headers=ORIGIN, json={**payload, "budget_usd": "9"}
    )
    assert response.status_code == 200
    assert Decimal(response.json()["budget_usd"]) == 9
    assert Decimal(response.json()["committed_cost_usd"]) == 2
    assert Decimal(response.json()["approved_cap_usd"]) == 5


@pytest.mark.parametrize("kind", ["paid", "render"])
def test_archive_refuses_unresolved_work(library, kind):
    p = library
    with p.api.sessions() as db:
        if kind == "paid":
            version = db.scalar(
                select(SceneVersion).join(Scene).where(Scene.project_id == UUID(p.project_id))
            )
            db.add(
                GenerationJob(
                    project_id=UUID(p.project_id),
                    scene_version_id=version.id,
                    model_id=WAN,
                    provider="fal",
                    endpoint_id=WAN,
                    idempotency_key=str(uuid4()),
                    status="reconciliation_required",
                )
            )
        else:
            db.add(
                RenderingJob(
                    project_id=UUID(p.project_id), idempotency_key=str(uuid4()), status="running"
                )
            )
        db.commit()
    response = p.api.client.post(
        p.base + "/archive",
        headers=ORIGIN,
        json={"expected_updated_at": p.items()["items"][0]["updated_at"], "archived": True},
    )
    assert response.status_code == 409 and response.json()["detail"].startswith("unresolved_")


def test_private_template_plan_is_portable_and_apply_resets_lineage(library):
    p = library
    saved = p.template()
    assert saved.status_code == 201, saved.text
    template = saved.json()
    assert not template["shared"] and template["owned"] and template["scene_count"] == 2
    assert template["model_ids"] == sorted([WAN, KLING])
    plan = template["production_plan"]
    serialized = json.dumps(plan)
    assert "fixture-secret" not in serialized and "fixture-credential" not in serialized
    assert "/private/source" not in serialized
    assert all(s["id"] is None and s["reference_asset_ids"] == [] for s in plan["scenes"])
    key = str(uuid4())
    response = p.api.client.post(
        f"/api/v1/templates/{template['id']}/apply",
        headers=ORIGIN,
        json={"name": "Template instance", "budget_usd": "3", "idempotency_key": key},
    )
    assert response.status_code == 201, response.text
    assert (
        Decimal(response.json()["committed_cost_usd"]) == 0
        and response.json()["approved_cap_usd"] is None
    )
    repeat = p.api.client.post(
        f"/api/v1/templates/{template['id']}/apply",
        headers=ORIGIN,
        json={"name": "Template instance", "budget_usd": "3", "idempotency_key": key},
    )
    assert repeat.json()["id"] == response.json()["id"]
    listed = p.api.client.get("/api/v1/templates").json()
    assert listed["total"] == 1 and "production_plan" not in listed["items"][0]


def test_template_sharing_requires_admin_explicit_ack_and_visibility(library, login):
    p = library
    private = p.template().json()
    assert p.template(shared=True, acknowledge_shared_content=True).status_code == 403
    login("other")
    assert p.api.client.get(f"/api/v1/templates/{private['id']}").status_code == 404
    assert p.api.client.get("/api/v1/templates").json()["total"] == 0
    assert (
        p.api.client.post(
            f"/api/v1/templates/{private['id']}/apply",
            headers=ORIGIN,
            json={"name": "No access", "budget_usd": "1", "idempotency_key": str(uuid4())},
        ).status_code
        == 404
    )
    login("admin")
    assert p.template(shared=True).status_code == 403
    shared = p.template(shared=True, acknowledge_shared_content=True).json()
    login("other")
    assert p.api.client.get(f"/api/v1/templates/{shared['id']}").status_code == 200
    assert p.api.client.get("/api/v1/templates").json()["total"] == 1
    assert (
        p.api.client.patch(
            f"/api/v1/templates/{shared['id']}",
            headers=ORIGIN,
            json={"expected_revision": 1, "name": "Cannot overwrite"},
        ).status_code
        == 404
    )
    assert (
        p.api.client.post(
            f"/api/v1/templates/{shared['id']}/apply",
            headers=ORIGIN,
            json={"name": "Shared instance", "budget_usd": "1", "idempotency_key": str(uuid4())},
        ).status_code
        == 201
    )


def test_template_metadata_revision_archive_and_idempotency(library):
    p = library
    key = str(uuid4())
    saved = p.template(idempotency_key=key).json()
    assert p.template(idempotency_key=key).json()["id"] == saved["id"]
    assert p.template(idempotency_key=key, name="Changed").status_code == 409
    route = f"/api/v1/templates/{saved['id']}"
    updated = p.api.client.patch(
        route,
        headers=ORIGIN,
        json={"expected_revision": 1, "name": "Renamed template", "archived": True},
    )
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    assert (
        p.api.client.patch(
            route, headers=ORIGIN, json={"expected_revision": 1, "description": "Stale"}
        ).status_code
        == 409
    )
    assert p.api.client.get("/api/v1/templates").json()["total"] == 0
    assert p.api.client.get("/api/v1/templates?archived=true").json()["total"] == 1
    assert (
        p.api.client.post(
            route + "/apply",
            headers=ORIGIN,
            json={"name": "Archived instance", "budget_usd": "1", "idempotency_key": str(uuid4())},
        ).status_code
        == 409
    )


def test_template_plan_injection_is_atomic(library):
    p = library
    saved = p.template().json()
    with p.api.sessions() as db:
        template = db.get(ProjectTemplate, UUID(saved["id"]))
        template.production_plan = {**template.production_plan, "credential": "must-not-expose"}
        before = db.scalar(select(func.count()).select_from(Project))
        db.commit()
    route = f"/api/v1/templates/{saved['id']}"
    assert p.api.client.get(route).status_code == 422
    response = p.api.client.post(
        route + "/apply",
        headers=ORIGIN,
        json={"name": "Rejected", "budget_usd": "1", "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 422 and "must-not-expose" not in response.text
    with p.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Project)) == before


def test_duplicate_late_failure_rolls_back_all_copied_rows(library, monkeypatch):
    from app import library as service
    from app.generation import GenerationError

    p = library
    tables = (Project, ProjectSettings, ProjectInput, Scene, SceneVersion, Storyboard)
    with p.api.sessions() as db:
        counts = {table: db.scalar(select(func.count()).select_from(table)) for table in tables}

    def reject_mutation(*_args, **_kwargs):
        raise GenerationError("offline_late_failure")

    monkeypatch.setattr(service, "_record_mutation", reject_mutation)
    response = p.duplicate()
    assert response.status_code == 409 and response.json()["detail"] == "offline_late_failure"
    with p.api.sessions() as db:
        for table, count in counts.items():
            assert db.scalar(select(func.count()).select_from(table)) == count
        assert db.scalar(select(func.count()).select_from(WorkspaceMutation)) == 0
        assert db.get(Project, UUID(p.project_id)).committed_cost_usd == 2


def test_missing_template_model_and_oversized_summary_are_rejected(library):
    p = library
    saved = p.template().json()
    identifier = UUID(saved["id"])
    plan = saved["production_plan"]
    plan["scenes"][0]["video_model_id"] = "offline-unknown-model"
    plan["summary"]["model_ids"] = sorted([WAN, KLING, "offline-unknown-model"])
    with p.api.sessions() as db:
        db.get(ProjectTemplate, identifier).production_plan = plan
        before = db.scalar(select(func.count()).select_from(Project))
        db.commit()
    route = f"/api/v1/templates/{identifier}"
    response = p.api.client.post(
        route + "/apply",
        headers=ORIGIN,
        json={"name": "Unknown model instance", "budget_usd": "1", "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 422 and response.json()["detail"] == "template_model_not_found"
    with p.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Project)) == before
        plan["summary"]["model_ids"] = ["x" * 161]
        db.get(ProjectTemplate, identifier).production_plan = plan
        db.commit()
    listed = p.api.client.get("/api/v1/templates").json()["items"][0]
    assert listed["plan_invalid"] and listed["model_ids"] == []
    assert p.api.client.get(route).status_code == 422
    archived = p.api.client.patch(
        route,
        headers=ORIGIN,
        json={"expected_revision": 1, "archived": True},
    )
    assert archived.status_code == 200 and archived.json()["plan_invalid"]


def test_incomplete_duration_draft_is_copied_without_generation(library):
    p = library
    with p.api.sessions() as db:
        first = db.scalar(select(Scene).where(Scene.project_id == UUID(p.project_id)))
        first.duration_seconds = 600
        db.commit()
    copied = p.duplicate()
    assert copied.status_code == 201
    with p.api.sessions() as db:
        project_id = UUID(copied.json()["id"])
        durations = db.scalars(select(Scene.duration_seconds).where(Scene.project_id == project_id))
        assert 600 in durations.all()
        assert not db.scalar(select(GenerationJob.id).where(GenerationJob.project_id == project_id))


@pytest.mark.parametrize(
    "suffix,payload",
    [
        ("/duplicate", {"name": "  ", "idempotency_key": "fixture-" * 3}),
        ("/duplicate", {"name": "Valid", "idempotency_key": "bad"}),
        (
            "/templates",
            {"name": "Valid", "idempotency_key": "fixture-" * 3, "shared": "true"},
        ),
        (
            "/metadata",
            {"name": "Valid", "budget_usd": "10001", "expected_updated_at": "2026-11-06T00:00:00Z"},
        ),
        (
            "/metadata",
            {"name": "Valid", "budget_usd": "1", "expected_updated_at": "2026-11-06T00:00:00"},
        ),
    ],
)
def test_workspace_mutation_contracts_reject_unsafe_inputs(library, suffix, payload):
    method = library.api.client.patch if suffix == "/metadata" else library.api.client.post
    response = method(library.base + suffix, headers=ORIGIN, json=payload)
    assert response.status_code == 422


def test_disabled_model_is_kept_as_draft_not_enabled(library):
    from app.models import VideoModel

    p = library
    with p.api.sessions() as db:
        db.scalar(select(VideoModel).where(VideoModel.model_id == WAN)).enabled = False
        db.commit()
    saved = p.template()
    assert saved.status_code == 201
    copied = p.duplicate()
    assert copied.status_code == 201
    with p.api.sessions() as db:
        assert db.scalar(select(VideoModel).where(VideoModel.model_id == WAN)).enabled is False
        assert all(
            s.status == "draft"
            for s in db.scalars(select(Scene).where(Scene.project_id == UUID(copied.json()["id"])))
        )


def test_viewer_cannot_modify_or_reuse_workspace(library, login):
    p = library
    saved = p.template().json()
    login("viewer")
    assert p.duplicate().status_code == 403
    assert p.template().status_code == 403
    assert (
        p.api.client.patch(
            f"/api/v1/templates/{saved['id']}",
            headers=ORIGIN,
            json={"expected_revision": 1, "name": "Viewer edit"},
        ).status_code
        == 403
    )
    assert (
        p.api.client.post(
            f"/api/v1/templates/{saved['id']}/apply",
            headers=ORIGIN,
            json={"name": "Viewer instance", "budget_usd": "1", "idempotency_key": str(uuid4())},
        ).status_code
        == 403
    )


def test_latest_output_is_private_and_uses_current_plan_revision(library):
    p = library
    with p.api.sessions() as db:
        project_id = UUID(p.project_id)
        board = db.scalar(select(Storyboard).where(Storyboard.project_id == project_id))
        asset_id = uuid4()
        db.add(
            MediaAsset(
                id=asset_id,
                project_id=project_id,
                kind="final_video",
                object_key=f"{project_id}/productions/{asset_id}.mp4",
                mime_type="video/mp4",
                size_bytes=1,
                sha256="a" * 64,
                status="ready",
            )
        )
        db.flush()
        db.add(
            RenderingJob(
                project_id=project_id,
                idempotency_key=str(uuid4()),
                status="succeeded",
                output_asset_id=asset_id,
                composition_snapshot={
                    "source": {"revision": board.revision, "target_duration_seconds": 10}
                },
            )
        )
        db.commit()
    output = p.items()["items"][0]["latest_output"]
    assert output["is_current"] and output["preview_url"] == f"/api/v1/assets/{asset_id}/preview"
    with p.api.sessions() as db:
        db.scalar(
            select(Storyboard).where(Storyboard.project_id == UUID(p.project_id))
        ).revision += 1
        db.commit()
    assert not p.items()["items"][0]["latest_output"]["is_current"]
