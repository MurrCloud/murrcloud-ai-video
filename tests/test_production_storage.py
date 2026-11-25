"""Private artifact quotas, immutable publication and mock-only S3 contracts."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.production_storage import S3Archive, artifact_path, publish_file
from app.storage import StorageError


@pytest.fixture
def archive_settings(api, tmp_path):
    settings = api.settings.model_copy(
        update={
            "storage_dir": tmp_path / "media",
            "storage_min_free_bytes": 1024**2,
            "storage_max_bytes": 1024**2,
            "s3_enabled": True,
            "s3_bucket": "private-test-bucket",
        }
    )
    source = tmp_path / "final.mp4"
    source.write_bytes(b"test-only-private-artifact")
    stored = publish_file(settings, source, uuid4(), uuid4(), "mp4", 1024**2)
    return SimpleNamespace(
        settings=settings,
        source=source,
        stored=stored,
        path=artifact_path(settings, stored.storage_key),
    )


def test_publication_is_bounded_immutable_and_private(archive_settings):
    a = archive_settings
    project, _, filename = a.stored.storage_key.split("/")
    asset_id = filename.removesuffix(".mp4")
    assert publish_file(a.settings, a.source, project, asset_id, "mp4", 1024**2) == a.stored
    a.source.write_bytes(b"changed")
    with pytest.raises(StorageError, match="artifact_publication_conflict"):
        publish_file(a.settings, a.source, project, asset_id, "mp4", 1024**2)
    assert a.path.read_bytes() == b"test-only-private-artifact"
    with pytest.raises(StorageError, match="storage_quota_exceeded"):
        publish_file(
            a.settings.model_copy(update={"storage_max_bytes": 1}),
            a.source,
            uuid4(),
            uuid4(),
            "mp4",
            1024**2,
        )
    for key in (
        "https://evil.invalid/file.mp4",
        "../../etc/passwd",
        f"{project}/productions/../x.mp4",
    ):
        with pytest.raises(StorageError):
            artifact_path(a.settings, key)


def test_archive_disabled_performs_no_remote_operation(archive_settings):
    class Forbidden:
        def __getattr__(self, _):
            pytest.fail("Disabled archive attempted a remote call")

    a = archive_settings
    result = S3Archive(a.settings.model_copy(update={"s3_enabled": False}), Forbidden()).archive(
        a.path, a.stored.storage_key, "video/mp4", a.stored.sha256, a.stored.size_bytes
    )
    assert result == {"status": "disabled"}


def test_private_s3_archive_conditional_put_and_duplicate_reconciliation(archive_settings):
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import ANY, Stubber

    a = archive_settings
    client = boto3.client(
        "s3",
        endpoint_url="https://storage.example.test",
        aws_access_key_id="offline-fixture",
        aws_secret_access_key="offline-fixture",
        region_name="us-east-1",
    )
    params = {
        "Bucket": "private-test-bucket",
        "Key": a.stored.storage_key,
        "Body": ANY,
        "ContentLength": a.stored.size_bytes,
        "ContentType": "video/mp4",
        "Metadata": {"sha256": a.stored.sha256},
        "IfNoneMatch": "*",
        "CacheControl": "private, no-store",
    }
    with Stubber(client) as stub:
        stub.add_response("put_object", {"ETag": '"offline"'}, params)
        stub.add_client_error(
            "put_object", "PreconditionFailed", http_status_code=412, expected_params=params
        )
        stub.add_response(
            "head_object",
            {"ContentLength": a.stored.size_bytes, "Metadata": {"sha256": a.stored.sha256}},
            {"Bucket": "private-test-bucket", "Key": a.stored.storage_key},
        )
        archive = S3Archive(a.settings, client)
        for _ in range(2):
            assert (
                archive.archive(
                    a.path, a.stored.storage_key, "video/mp4", a.stored.sha256, a.stored.size_bytes
                )["status"]
                == "archived"
            )
        stub.assert_no_pending_responses()
    client.close()


def test_archive_rejects_source_mutation_and_remote_conflicts(archive_settings):
    pytest.importorskip("boto3")
    from botocore.exceptions import ClientError

    a = archive_settings

    class Conflict:
        def put_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")

        def head_object(self, **kwargs):
            return {"ContentLength": 99, "Metadata": {"sha256": "wrong"}}

    with pytest.raises(StorageError, match="archive_object_conflict"):
        S3Archive(a.settings, Conflict()).archive(
            a.path, a.stored.storage_key, "video/mp4", a.stored.sha256, a.stored.size_bytes
        )
    a.path.write_bytes(b"mutated")
    with pytest.raises(StorageError, match="archive_source_changed"):
        S3Archive(a.settings, Conflict()).archive(
            a.path, a.stored.storage_key, "video/mp4", a.stored.sha256, a.stored.size_bytes
        )


def test_archive_transport_deadline_and_secret_isolation(archive_settings, monkeypatch):
    import json
    import subprocess

    from pydantic import SecretStr

    a = archive_settings
    settings = a.settings.model_copy(
        update={
            "s3_endpoint_url": "https://storage.example.test",
            "s3_region": "us-east-1",
            "s3_access_key_id": SecretStr("test-archive-access"),
            "s3_secret_access_key": SecretStr("test-archive-secret"),
        }
    )
    monkeypatch.setenv("VIDEO_OPENAI_API_KEY", "must-not-reach-child")

    def timeout(args, **kwargs):
        assert args[-1] == "app.archive_worker"
        assert kwargs["timeout"] == 90 and kwargs["start_new_session"]
        assert "VIDEO_OPENAI_API_KEY" not in kwargs["env"]
        request = json.loads(kwargs["input"])
        assert request["secret_key"] == "test-archive-secret"
        assert "test-archive-secret" not in str(args)
        raise subprocess.TimeoutExpired(args, 90)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(StorageError, match="archive_timeout"):
        S3Archive(settings).archive(
            a.path, a.stored.storage_key, "video/mp4", a.stored.sha256, a.stored.size_bytes
        )
