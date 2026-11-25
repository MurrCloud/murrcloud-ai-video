from uuid import uuid4

from app.catalog import seed_catalog
from app.models import MediaAsset, Project, ProviderConfiguration, VideoModel
from conftest import ORIGIN
from pydantic import SecretStr
from sqlalchemy import select


def seed(api):
    with api.sessions() as db:
        rows = seed_catalog(db)
        db.commit()
        return [row.model_id for row in rows]


def test_model_catalog_is_authenticated_and_projects_safe_metadata(api, login):
    assert api.client.get("/api/v1/models").status_code == 401
    ids = seed(api)
    with api.sessions() as db:
        row = db.scalar(select(VideoModel).where(VideoModel.model_id == ids[0]))
        row.pricing_details = {**row.pricing_details, "private_api_key": "catalog-secret-sentinel"}
        db.commit()
    login()
    response = api.client.get("/api/v1/models")
    assert response.status_code == 200
    assert len(response.json()) == 4
    assert sum(row["enabled"] for row in response.json()) == 2
    assert "catalog-secret-sentinel" not in response.text
    assert all(row["verified_at"] and row["documentation_url"] for row in response.json())


def test_disabled_provider_removes_model_availability(api, login):
    seed(api)
    with api.sessions() as db:
        db.scalar(select(ProviderConfiguration)).enabled = False
        db.commit()
    login()
    assert not any(row["enabled"] for row in api.client.get("/api/v1/models").json())


def test_model_switches_require_admin_and_preserve_unique_default(api, login):
    ids = seed(api)
    login()
    target = next(value for value in ids if "kling" in value and "text-to-video" in value)
    assert (
        api.client.patch(
            f"/api/v1/models/{target}", json={"is_default": True}, headers={"Origin": ORIGIN}
        ).status_code
        == 403
    )
    api.client.cookies.clear()
    login("admin")
    response = api.client.patch(
        f"/api/v1/models/{target}", json={"is_default": True}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200
    assert [
        row["model_id"] for row in api.client.get("/api/v1/models").json() if row["is_default"]
    ] == [target]
    assert (
        api.client.patch(
            f"/api/v1/models/{target}", json={"enabled": False}, headers={"Origin": ORIGIN}
        ).status_code
        == 200
    )
    image = next(value for value in ids if "image-to-video" in value)
    assert (
        api.client.patch(
            f"/api/v1/models/{image}", json={"enabled": True}, headers={"Origin": ORIGIN}
        ).status_code
        == 409
    )


def test_generation_configuration_never_returns_provider_credentials(api, login):
    api.settings.fal_key = SecretStr("provider-secret-sentinel")
    login()
    response = api.client.get("/api/v1/generation-config")
    assert response.json() == {
        "paid_generation_enabled": False,
        "provider_configured": True,
        "requires_operator_approval": True,
        "reference_uploads_available": False,
    }
    assert "provider-secret-sentinel" not in response.text


def test_local_clip_download_is_owner_scoped(api, login, tmp_path):
    api.settings.storage_dir = tmp_path.resolve()
    project_id, job_id = uuid4(), uuid4()
    folder = tmp_path / str(project_id)
    folder.mkdir()
    clip = folder / f"{job_id}.mp4"
    clip.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 52)
    with api.sessions() as db:
        db.add(Project(id=project_id, owner_id=api.user_ids["editor"], name="Protected clip"))
        db.flush()
        asset = MediaAsset(
            project_id=project_id,
            kind="clip",
            object_key=f"{project_id}/{job_id}.mp4",
            mime_type="video/mp4",
            size_bytes=64,
            status="ready",
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id
    login()
    response = api.client.get(f"/api/v1/assets/{asset_id}/download")
    assert response.status_code == 200 and response.content == clip.read_bytes()
    assert "attachment" in response.headers["content-disposition"]
    api.client.cookies.clear()
    login("other")
    assert api.client.get(f"/api/v1/assets/{asset_id}/download").status_code == 404


def test_tampered_storage_path_does_not_read_outside_storage(api, login):
    with api.sessions() as db:
        project = Project(owner_id=api.user_ids["editor"], name="Tampered asset")
        db.add(project)
        db.flush()
        asset = MediaAsset(
            project_id=project.id,
            kind="clip",
            object_key="../../etc/passwd",
            mime_type="video/mp4",
            size_bytes=64,
            status="ready",
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id
    login()
    assert api.client.get(f"/api/v1/assets/{asset_id}/download").status_code == 404
