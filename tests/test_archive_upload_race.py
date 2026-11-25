"""Archive and music-upload admission share a locked project boundary."""

from uuid import UUID

from app.library import archive_project
from app.library_schemas import ArchiveIn
from app.models import AuditEvent, MediaAsset, Project, User
from app.security import aware_utc
from sqlalchemy import func, select
from starlette.datastructures import UploadFile

ORIGIN = {"Origin": "http://localhost:3000"}


def test_archive_during_music_read_blocks_new_durable_intent(api, login, monkeypatch):
    login()
    response = api.client.post(
        "/api/v1/projects", headers=ORIGIN, json={"name": "Upload/archive race"}
    )
    assert response.status_code == 201
    project_id = UUID(response.json()["id"])
    original_read = UploadFile.read
    observed = {"archived": False, "published": False}

    async def archive_before_read_returns(upload, size=-1):
        data = await original_read(upload, size)
        with api.sessions() as db:
            project = db.get(Project, project_id)
            archive_project(
                db,
                project_id,
                db.get(User, api.user_ids["editor"]),
                ArchiveIn(expected_updated_at=aware_utc(project.updated_at), archived=True),
            )
        observed["archived"] = True
        return data

    def forbidden_publication(*_args, **_kwargs):
        observed["published"] = True
        raise AssertionError("An upload archived before intent must never publish media")

    monkeypatch.setattr(UploadFile, "read", archive_before_read_returns)
    monkeypatch.setattr("app.production_api.publish_file", forbidden_publication)
    response = api.client.post(
        f"/api/v1/projects/{project_id}/assets/music",
        headers=ORIGIN,
        files={"file": ("music.wav", b"RIFF\x00\x00\x00\x00WAVEdata", "audio/wav")},
    )
    assert observed["archived"] and not observed["published"]
    assert response.status_code == 409 and response.json()["detail"] == "project_archived"
    with api.sessions() as db:
        assert db.get(Project, project_id).archived
        assert (
            db.scalar(
                select(func.count())
                .select_from(MediaAsset)
                .where(MediaAsset.project_id == project_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.project_id == project_id, AuditEvent.event_type == "music.uploaded"
                )
            )
            == 0
        )
