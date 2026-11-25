import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app import ingestion_api
from app.ingestion import CanonicalImage
from app.models import MediaAsset
from app.reference_storage import remove_uncommitted_reference, store_reference
from app.storage import StorageError
from conftest import ORIGIN
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session


@pytest.fixture
def reference_setup(api, login, tmp_path):
    login()
    result = api.client.post(
        "/api/v1/projects", json={"name": "Reference cleanup"}, headers={"Origin": ORIGIN}
    )
    assert result.status_code == 201
    api.settings.storage_dir = tmp_path / "media"
    # The disposable test container has a 256 MiB tmpfs. Exercise transaction
    # cleanup with a fixture reserve that fits it; production keeps 512 MiB.
    api.settings.storage_min_free_bytes = 1024**2
    body = b"fixture-canonical-image"
    image = CanonicalImage(body, "jpg", "image/jpeg", 300, 300, hashlib.sha256(body).hexdigest())
    return SimpleNamespace(project_id=UUID(result.json()["id"]), image=image)


def request_for(api):
    return SimpleNamespace(app=api.app)


def publish(api, setup):
    asset_id = uuid4()
    key = store_reference(setup.image, setup.project_id, asset_id, api.settings)
    return asset_id, key, api.settings.storage_dir / key


def upload(api, setup, monkeypatch):
    monkeypatch.setattr(ingestion_api, "canonicalize_reference", lambda *_args: setup.image)
    return api.client.post(
        f"/api/v1/projects/{setup.project_id}/assets/reference-images",
        files={"file": ("reference.jpg", b"fixture-source", "image/jpeg")},
        headers={"Origin": ORIGIN},
    )


def test_failed_upload_commit_removes_only_its_unclaimed_reference(
    api, reference_setup, monkeypatch
):
    original_commit = Session.commit

    def fail_asset_commit(db):
        if any(isinstance(row, MediaAsset) for row in db.new):
            raise OperationalError("upload transaction", None, RuntimeError("private-db-secret"))
        return original_commit(db)

    monkeypatch.setattr(Session, "commit", fail_asset_commit)
    response = upload(api, reference_setup, monkeypatch)
    assert response.status_code == 503
    assert "private-db-secret" not in response.text
    assert list(api.settings.storage_dir.rglob("*.jpg")) == []
    with api.sessions() as db:
        assert db.scalar(select(MediaAsset)) is None


def test_ambiguous_committed_upload_keeps_its_file(api, reference_setup, monkeypatch):
    original_commit = Session.commit

    def commit_with_lost_ack(db):
        has_asset = any(isinstance(row, MediaAsset) for row in db.new)
        original_commit(db)
        if has_asset:
            raise OperationalError(
                "commit acknowledgement", None, RuntimeError("private-db-secret")
            )

    monkeypatch.setattr(Session, "commit", commit_with_lost_ack)
    response = upload(api, reference_setup, monkeypatch)
    assert response.status_code == 503
    with api.sessions() as db:
        asset = db.scalar(select(MediaAsset))
        assert asset is not None
        assert (
            api.settings.storage_dir / asset.object_key
        ).read_bytes() == reference_setup.image.data


def test_database_unavailable_during_reconciliation_retains_file_and_safe_notice(
    api, reference_setup, monkeypatch, caplog
):
    original_commit = Session.commit

    def unavailable():
        raise OperationalError("reconciliation", None, RuntimeError("private-db-secret"))

    def fail_asset_commit(db):
        if any(isinstance(row, MediaAsset) for row in db.new):
            monkeypatch.setattr(api.app.state, "sessions", unavailable)
            raise OperationalError("upload transaction", None, RuntimeError("private-db-secret"))
        return original_commit(db)

    monkeypatch.setattr(Session, "commit", fail_asset_commit)
    response = upload(api, reference_setup, monkeypatch)
    assert response.status_code == 503
    assert len(list(api.settings.storage_dir.rglob("*.jpg"))) == 1
    assert "operator reconciliation required" in caplog.text
    assert "private-db-secret" not in caplog.text + response.text
    with api.sessions() as db:
        assert db.scalar(select(MediaAsset)) is None


def test_fresh_database_check_precedes_file_deletion(api, reference_setup, monkeypatch):
    asset_id, key, path = publish(api, reference_setup)
    opened = []
    original_factory = api.sessions

    def fresh_factory():
        session = original_factory()
        opened.append(session)
        return session

    monkeypatch.setattr(api.app.state, "sessions", fresh_factory)
    with original_factory() as old:
        assert (
            ingestion_api._cleanup_failed_reference(
                request_for(api), old, reference_setup.project_id, asset_id, key
            )
            == "removed"
        )
        assert opened and opened[0] is not old
    assert not path.exists()


def test_reference_cleanup_is_idempotent_and_does_not_delete_neighbors(api, reference_setup):
    _asset, key, path = publish(api, reference_setup)
    _, _, neighboring = publish(api, reference_setup)
    assert remove_uncommitted_reference(api.settings, key) is True
    assert remove_uncommitted_reference(api.settings, key) is False
    assert not path.exists() and neighboring.exists()


def test_reference_cleanup_refuses_symlinks_and_invalid_paths(api, reference_setup, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"keep outside data")
    _, key, path = publish(api, reference_setup)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(StorageError, match="unsafe_storage_entry"):
        remove_uncommitted_reference(api.settings, key)
    assert path.is_symlink() and outside.read_bytes() == b"keep outside data"
    with pytest.raises(StorageError, match="invalid_reference_key"):
        remove_uncommitted_reference(api.settings, "../../outside.jpg")
    assert outside.exists()


def test_reference_cleanup_busy_quota_lock_defers_without_deleting(
    api, reference_setup, monkeypatch, caplog
):
    import app.reference_storage as storage

    asset_id, key, path = publish(api, reference_setup)

    def busy(*_args):
        raise BlockingIOError

    monkeypatch.setattr(storage.fcntl, "flock", busy)
    with api.sessions() as old:
        assert (
            ingestion_api._cleanup_failed_reference(
                request_for(api), old, reference_setup.project_id, asset_id, key
            )
            == "deferred"
        )
    assert path.exists()
    assert "operator reconciliation required" in caplog.text
