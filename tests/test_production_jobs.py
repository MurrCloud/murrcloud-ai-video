"""Offline production snapshot, authorization, lease and recovery regressions.

Fake speech/media adapters below test orchestration only. They are not evidence
of speech quality, FFmpeg output validation or live provider acceptance.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app.catalog import seed_catalog
from app.models import (
    GenerationJob,
    MediaAsset,
    NarrationAsset,
    Project,
    ProjectSettings,
    RenderingJob,
    Scene,
    SceneVersion,
    Storyboard,
    SubtitleAsset,
)
from app.production_api import router
from app.production_schemas import ProductionSettings
from app.production_storage import artifact_path
from app.production_tasks import (
    claim,
    media_lock,
    prepare_render,
    reconcile_production,
    render_video,
)
from app.storage import StorageError
from app.tts import SpeechBoundary, SpeechResult
from sqlalchemy import delete, func, select

ORIGIN = {"Origin": "http://localhost:3000"}
WAN = "fal-ai/wan/v2.7/text-to-video"


@pytest.fixture
def production(api, login, tmp_path, monkeypatch):
    if "/api/v1/projects/{project_id}/production" not in api.app.openapi()["paths"]:
        api.app.include_router(router)
    login()
    settings = api.settings.model_copy(
        update={
            "render_enabled": True,
            "narration_enabled": True,
            "storage_dir": tmp_path / "media",
            "temp_dir": tmp_path / "scratch",
            "storage_min_free_bytes": 1024**2,
            "storage_max_bytes": 32 * 1024**2,
            "max_media_bytes": 4 * 1024**2,
            "render_timeout_seconds": 30,
            "render_lease_seconds": 120,
            "render_max_attempts": 3,
        }
    )
    api.app.state.settings = settings
    queued = []
    api.app.state.production_dispatcher = lambda *args, **kwargs: queued.append((args, kwargs))

    # Each provider is injected below. A production retry must not reach any
    # cloud video/director or real speech adapter even with test gates enabled.
    async def forbidden(*args, **kwargs):
        pytest.fail("Production orchestration attempted an unmocked provider call")

    monkeypatch.setattr("app.providers.fal.FalProvider.submit_generation", forbidden)
    monkeypatch.setattr("app.director.OpenAIDirector.generate", forbidden)
    monkeypatch.setattr("app.tts.EdgeTTS.synthesize", forbidden)
    with api.sessions() as db:
        seed_catalog(db)
        db.commit()
    created = api.client.post(
        "/api/v1/projects",
        json={"name": "Offline production", "target_duration_seconds": 10, "budget_usd": "10"},
        headers=ORIGIN,
    )
    assert created.status_code == 201, created.text
    project_id = UUID(created.json()["id"])
    options = ProductionSettings(opening_title="Welcome", narration_enabled=True)
    scenes, versions, clip_ids = [], [], []
    with api.sessions() as db:
        row = db.scalar(select(ProjectSettings).where(ProjectSettings.project_id == project_id))
        row.composition = {"production": options.model_dump(mode="json")}
        db.add(Storyboard(project_id=project_id, revision=3, title="Saved production plan"))
        for order in (1, 2):
            scene = Scene(
                project_id=project_id,
                scene_key=f"scene_{order:03d}",
                order_index=order,
                duration_seconds=5,
                video_model_id=WAN,
                visual_prompt=f"Scene {order} sea",
                narration=f"Scene {order} narration",
                status="completed",
                generation_settings={
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                    "audio_enabled": True,
                },
            )
            db.add(scene)
            db.flush()
            version = SceneVersion(
                scene_id=scene.id,
                version_number=1,
                video_model_id=WAN,
                generation_mode="text_to_video",
                duration_seconds=5,
                visual_prompt=scene.visual_prompt,
                narration=scene.narration,
                status="completed",
            )
            db.add(version)
            db.flush()
            asset_id = uuid4()
            folder = settings.storage_dir / str(project_id)
            folder.mkdir(parents=True, exist_ok=True)
            data = b"\x00\x00\x00\x18ftypisom" + f"offline-clip-{order}".encode()
            (folder / f"{asset_id}.mp4").write_bytes(data)
            asset = MediaAsset(
                id=asset_id,
                project_id=project_id,
                scene_version_id=version.id,
                kind="clip",
                object_key=f"{project_id}/{asset_id}.mp4",
                mime_type="video/mp4",
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                status="ready",
                metadata_json={"width": 1280, "height": 720},
            )
            db.add(asset)
            db.flush()
            db.add(
                GenerationJob(
                    project_id=project_id,
                    scene_version_id=version.id,
                    model_id=WAN,
                    provider="fal",
                    endpoint_id=WAN,
                    idempotency_key=str(uuid4()),
                    status="succeeded",
                    request_snapshot={
                        "resolution": "720p",
                        "aspect_ratio": "16:9",
                        "audio_enabled": True,
                    },
                    output_asset_id=asset_id,
                )
            )
            scenes.append(scene.id)
            versions.append(version.id)
            clip_ids.append(asset_id)
        db.commit()
    base = f"/api/v1/projects/{project_id}"
    speech_calls, compositions, archives = [], [], []

    class FakeTTS:
        async def synthesize(self, text, voice, rate, language, *, approved=False):
            assert approved
            speech_calls.append((text, voice, rate, language))
            return SpeechResult(
                audio=b"ID3mock-speech-" + text.encode(),
                boundaries=[SpeechBoundary(0.1, 1.1, text)],
            )

        async def aclose(self):
            pass

    def fake_probe(path):
        return SimpleNamespace(duration_seconds=1.2, has_audio=True)

    def fake_compose(request):
        compositions.append(request)
        request.progress(40)
        request.output.write_bytes(b"\x00\x00\x00\x18ftypisom-offline-final-video")
        return SimpleNamespace(
            path=request.output,
            width=1920,
            height=1080,
            duration_seconds=12,
            report={"validated": True, "duration_seconds": 12},
            warnings=["output_exceeds_source_resolution"],
        )

    class FakeArchive:
        def __init__(self, _settings):
            pass

        def archive(self, path, key, mime, digest, size):
            archives.append((path, key, mime, digest, size))
            return {"status": "disabled"}

    def state():
        response = api.client.get(base + "/production")
        assert response.status_code == 200, response.text
        return response.json()

    def create(**changes):
        payload = {
            "expected_revision": state()["revision"],
            "idempotency_key": str(uuid4()),
            **changes,
        }
        return api.client.post(base + "/renders", json=payload, headers=ORIGIN)

    def prepare(identifier, **changes):
        return prepare_render(
            identifier,
            sessions=api.sessions,
            settings=settings,
            dispatcher=api.app.state.production_dispatcher,
            tts_factory=changes.pop("tts_factory", lambda _: FakeTTS()),
            admission=changes.pop("admission", lambda _: True),
            probe=changes.pop("probe", fake_probe),
            **changes,
        )

    def render(identifier, **changes):
        return render_video(
            identifier,
            sessions=api.sessions,
            settings=settings,
            dispatcher=api.app.state.production_dispatcher,
            admission=changes.pop("admission", lambda _: True),
            composer=changes.pop("composer", fake_compose),
            archive_factory=changes.pop("archive_factory", FakeArchive),
            **changes,
        )

    return SimpleNamespace(
        api=api,
        settings=settings,
        project_id=project_id,
        base=base,
        state=state,
        create=create,
        prepare=prepare,
        render=render,
        queued=queued,
        scenes=scenes,
        versions=versions,
        clip_ids=clip_ids,
        options=options,
        speech_calls=speech_calls,
        compositions=compositions,
        archives=archives,
    )


def identifier(response):
    assert response.status_code == 202, response.text
    return UUID(response.json()["id"])


def job(production, identifier):
    with production.api.sessions() as db:
        return db.get(RenderingJob, identifier)


def test_production_pipeline_is_idempotent_and_uses_only_selected_clips(production):
    studio = production
    original = studio.state()
    assert [row["selected_scene_version_id"] for row in original["scenes"]] == [
        str(v) for v in studio.versions
    ]
    key = str(uuid4())
    first = studio.create(idempotency_key=key)
    render_id = identifier(first)
    assert identifier(studio.create(idempotency_key=key)) == render_id
    assert studio.create().status_code == 409
    studio.prepare(render_id)
    prepared = job(studio, render_id)
    assert (
        prepared.stage == "rendering"
        and prepared.status == "pending"
        and prepared.progress_percent == 35
    )
    assert len(studio.speech_calls) == 2
    studio.prepare(render_id)
    assert len(studio.speech_calls) == 2
    studio.render(render_id)
    complete = job(studio, render_id)
    assert complete.status == "succeeded" and complete.progress_percent == 100
    assert complete.output_asset_id and complete.subtitle_asset_id
    assert complete.validation_report["validated"] is True
    assert len(studio.compositions) == 1
    assert all(scene.narration_path is not None for scene in studio.compositions[0].scenes)
    assert studio.compositions[0].opening_title == "Welcome"
    studio.render(render_id)
    assert len(studio.compositions) == 1
    state = studio.state()
    assert state["render_jobs"][0]["is_current"] is True
    assert state["render_jobs"][0]["preview_url"].endswith("/preview")
    with studio.api.sessions() as db:
        assert db.get(Project, studio.project_id).status == "completed"
        assert db.get(Project, studio.project_id).committed_cost_usd == Decimal("0")
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 2
        assert db.scalar(select(func.count()).select_from(SceneVersion)) == 2
        assert db.scalar(select(func.count()).select_from(NarrationAsset)) == 2
        assert db.scalar(select(func.count()).select_from(SubtitleAsset)) == 1
        captions = db.get(MediaAsset, complete.subtitle_asset_id)
        body = artifact_path(studio.settings, captions.object_key).read_text()
        assert "00:00:02,100 --> 00:00:03,100" in body
        assert "00:00:07,100 --> 00:00:08,100" in body
    assert not list(studio.settings.temp_dir.glob("render-*"))
    assert not list(studio.settings.temp_dir.glob("narration-*"))


def test_stale_output_is_preserved_without_marking_current_project_completed(production):
    studio = production
    render_id = identifier(studio.create())
    studio.prepare(render_id)
    current = studio.state()
    changed = studio.api.client.put(
        studio.base + "/production/settings",
        json={
            "expected_revision": current["revision"],
            "settings": {**current["settings"], "closing_title": "A later plan"},
        },
        headers=ORIGIN,
    )
    assert changed.status_code == 200
    studio.render(render_id)
    assert job(studio, render_id).status == "succeeded"
    result = studio.state()["render_jobs"][0]
    assert result["is_current"] is False and result["download_url"]
    with studio.api.sessions() as db:
        assert db.get(Project, studio.project_id).status == "draft"


def test_cancelled_render_cannot_publish_completed_job(production):
    studio = production
    render_id = identifier(studio.create())
    studio.prepare(render_id)

    def cancelling_compose(request):
        with studio.api.sessions() as db:
            db.get(RenderingJob, render_id).status = "cancelled"
            db.commit()
        request.progress(20)
        pytest.fail("Cancelled work continued after its lease check")

    studio.render(render_id, composer=cancelling_compose)
    row = job(studio, render_id)
    assert row.status == "cancelled" and row.output_asset_id is None
    assert studio.state()["render_jobs"][0]["download_url"] is None


@pytest.mark.parametrize("throws", [False, True])
def test_host_admission_fails_closed_before_tts_or_render(production, throws):
    studio = production
    render_id = identifier(studio.create())

    def admission(_):
        if throws:
            raise RuntimeError("private-host-probe-details")
        return False

    studio.prepare(render_id, admission=admission)
    row = job(studio, render_id)
    assert row.status == "paused" and row.error_code == "host_admission_refused"
    assert row.attempt_count == 0 and studio.speech_calls == []
    assert "private-host-probe-details" not in studio.api.client.get(studio.base + "/renders").text


def test_duplicate_claim_and_shared_render_lock_prevent_parallel_work(production):
    studio = production
    render_id = identifier(studio.create())
    first = claim(studio.api.sessions, studio.settings, render_id, "narration", lambda _: True)
    assert first is not None
    assert (
        claim(studio.api.sessions, studio.settings, render_id, "narration", lambda _: True) is None
    )
    with studio.api.sessions() as db:
        row = db.get(RenderingJob, render_id)
        row.status = "pending"
        row.lease_token = row.lease_expires_at = None
        db.commit()
    studio.prepare(render_id)
    with media_lock(studio.settings, ".render.lock"):
        studio.render(render_id)
    row = job(studio, render_id)
    assert row.status == "paused" and row.error_code == "media_worker_busy"
    assert studio.compositions == []


def test_expired_lease_recovers_even_when_rendering_is_disabled(production):
    studio = production
    render_id = identifier(studio.create())
    with studio.api.sessions() as db:
        row = db.get(RenderingJob, render_id)
        row.status = "running"
        row.lease_token = "old-worker-token"
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        row.attempt_count = 1
        db.commit()
    studio.queued.clear()
    disabled = studio.settings.model_copy(update={"render_enabled": False})
    reconcile_production(
        sessions=studio.api.sessions,
        settings=disabled,
        dispatcher=studio.api.app.state.production_dispatcher,
    )
    row = job(studio, render_id)
    assert row.status == "paused" and row.error_code == "worker_interrupted"
    assert row.lease_token is None and studio.queued == []


def test_corrupted_selected_clip_fails_before_composer(production):
    studio = production
    render_id = identifier(studio.create())
    studio.prepare(render_id)
    with studio.api.sessions() as db:
        asset = db.get(MediaAsset, studio.clip_ids[0])
        path = studio.settings.storage_dir / asset.object_key
        path.write_bytes(b"changed-after-approval")
    studio.render(render_id)
    row = job(studio, render_id)
    assert row.status == "failed" and row.error_code == "render_input_changed"
    assert studio.compositions == [] and row.output_asset_id is None


def test_completed_publication_is_reused_after_archive_failure(production):
    studio = production
    render_id = identifier(studio.create())
    studio.prepare(render_id)

    class FailingArchive:
        def __init__(self, _settings):
            pass

        def archive(self, *_):
            raise StorageError("archive_unavailable", retryable=True)

    studio.render(render_id, archive_factory=FailingArchive)
    assert job(studio, render_id).status == "paused" and len(studio.compositions) == 1
    with studio.api.sessions() as db:
        db.get(RenderingJob, render_id).next_attempt_at = None
        db.commit()
    studio.render(render_id)
    assert job(studio, render_id).status == "succeeded" and len(studio.compositions) == 1
    assert len(studio.speech_calls) == 2


@pytest.mark.parametrize("pending", [False, True])
def test_narration_publication_recovers_cache_gap_without_repeat_speech(production, pending):
    studio = production
    render_id = identifier(studio.create())
    studio.prepare(render_id)
    assert len(studio.speech_calls) == 2
    with studio.api.sessions() as db:
        cached = db.scalar(
            select(NarrationAsset).where(NarrationAsset.scene_version_id == studio.versions[0])
        )
        media_id = cached.media_asset_id
        db.execute(delete(NarrationAsset).where(NarrationAsset.id == cached.id))
        if pending:
            db.get(MediaAsset, media_id).status = "pending"
        row = db.get(RenderingJob, render_id)
        row.stage, row.status = "narration", "pending"
        row.composition_snapshot = {**row.composition_snapshot, "prepared": {}}
        row.attempt_count = 0
        db.commit()
    studio.prepare(render_id)
    row = job(studio, render_id)
    assert row.stage == "rendering" and row.status == "pending", row.error_code
    assert len(studio.speech_calls) == 2
    with studio.api.sessions() as db:
        assert db.get(MediaAsset, media_id).status == "ready"
        assert db.scalar(select(func.count()).select_from(NarrationAsset)) == 2


def test_production_api_enforces_roles_ownership_and_exact_revision(production, login):
    studio = production
    snapshot = studio.state()
    invalid = studio.api.client.put(
        studio.base + "/production/settings",
        json={
            "expected_revision": 0,
            "settings": snapshot["settings"],
        },
        headers=ORIGIN,
    )
    assert invalid.status_code == 409 and invalid.json()["detail"] == "revision_conflict"
    bad_voice = studio.api.client.put(
        studio.base + "/production/settings",
        json={
            "expected_revision": snapshot["revision"],
            "settings": {**snapshot["settings"], "voice": "https://169.254.169.254/private"},
        },
        headers=ORIGIN,
    )
    assert bad_voice.status_code == 422
    foreign_selection = studio.create(
        scene_versions={
            str(studio.scenes[0]): str(studio.versions[1]),
            str(studio.scenes[1]): str(studio.versions[0]),
        }
    )
    assert foreign_selection.status_code == 409
    render_id = identifier(studio.create())
    login("other")
    assert studio.api.client.get(studio.base + "/production").status_code == 404
    assert studio.api.client.get(studio.base + "/renders").status_code == 404
    assert (
        studio.api.client.post(
            studio.base + f"/renders/{render_id}/cancel", headers=ORIGIN
        ).status_code
        == 404
    )
    login("viewer")
    assert (
        studio.api.client.post(
            studio.base + "/renders",
            json={"expected_revision": 3, "idempotency_key": str(uuid4())},
            headers=ORIGIN,
        ).status_code
        == 403
    )
    login("editor")
    assert (
        studio.api.client.post(
            studio.base + f"/renders/{render_id}/cancel", headers=ORIGIN
        ).status_code
        == 202
    )
    assert job(studio, render_id).status == "cancelled"


def test_default_render_gate_does_not_reserve_or_dispatch_paid_work(production):
    studio = production
    studio.api.app.state.settings = studio.settings.model_copy(update={"render_enabled": False})
    response = studio.create()
    assert response.status_code == 409 and response.json()["detail"] == "rendering_disabled"
    assert studio.queued == []
    with studio.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(RenderingJob)) == 0
        assert db.get(Project, studio.project_id).committed_cost_usd == 0


def test_corrupted_ready_narration_fails_without_regenerating_speech(production):
    studio = production
    render_id = identifier(studio.create())
    studio.prepare(render_id)
    with studio.api.sessions() as db:
        cached = db.scalar(
            select(NarrationAsset).where(NarrationAsset.scene_version_id == studio.versions[0])
        )
        media = db.get(MediaAsset, cached.media_asset_id)
        artifact_path(studio.settings, media.object_key).write_bytes(b"corrupt-cache")
        row = db.get(RenderingJob, render_id)
        row.stage, row.status = "narration", "pending"
        row.attempt_count = 0
        row.composition_snapshot = {**row.composition_snapshot, "prepared": {}}
        db.commit()
    studio.prepare(render_id)
    assert job(studio, render_id).status == "failed"
    assert len(studio.speech_calls) == 2


def test_pending_final_publication_recovers_without_recomposition(production):
    studio = production
    render_id = identifier(studio.create())
    studio.prepare(render_id)
    studio.render(render_id)
    assert len(studio.compositions) == 1
    with studio.api.sessions() as db:
        row = db.get(RenderingJob, render_id)
        media = db.get(MediaAsset, row.output_asset_id)
        media.status = "pending"
        row.status, row.stage = "paused", "rendering"
        row.output_asset_id = None
        row.next_attempt_at = None
        row.attempt_count = 1
        row.progress_percent = 95
        db.commit()
    studio.render(render_id)
    row = job(studio, render_id)
    assert row.status == "succeeded", row.error_code
    assert len(studio.compositions) == 1


def test_private_preview_download_and_srt_access(production, login):
    studio = production
    render_id = identifier(studio.create())
    studio.prepare(render_id)
    studio.render(render_id)
    rendered = studio.state()["render_jobs"][0]
    preview = studio.api.client.get(rendered["preview_url"], headers={"Range": "bytes=0-11"})
    assert preview.status_code == 206 and len(preview.content) == 12
    assert preview.headers["content-type"] == "video/mp4"
    assert preview.headers["cache-control"] == "no-store"
    assert "inline" in preview.headers["content-disposition"]
    downloaded = studio.api.client.get(rendered["download_url"])
    assert (
        downloaded.status_code == 200 and "attachment" in downloaded.headers["content-disposition"]
    )
    captions = studio.api.client.get(rendered["srt_url"])
    assert captions.status_code == 200 and "Scene 1 narration" in captions.text
    assert "application/x-subrip" in captions.headers["content-type"]
    login("other")
    for path in (rendered["preview_url"], rendered["download_url"], rendered["srt_url"]):
        assert studio.api.client.get(path).status_code == 404


def test_music_upload_validates_signature_size_and_filename(production, login):
    studio = production
    path = studio.base + "/assets/music"
    valid = b"RIFF" + bytes(4) + b"WAVE" + b"bounded-quarantined-media" * 10_000
    uploaded = studio.api.client.post(
        path, files={"file": ("../../private/music.wav", valid, "audio/wav")}, headers=ORIGIN
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["original_filename"] == "music.wav" and "object_key" not in body
    with studio.api.sessions() as db:
        asset = db.get(MediaAsset, UUID(body["id"]))
        assert asset.status == "ready" and asset.metadata_json["probe_required"] is True
        assert artifact_path(studio.settings, asset.object_key).read_bytes() == valid
    assert (
        studio.api.client.post(
            path, files={"file": ("bad.wav", b"not-wave", "audio/wav")}, headers=ORIGIN
        ).status_code
        == 415
    )
    assert (
        studio.api.client.post(
            path, files={"file": ("bad.svg", b"<svg/>", "image/svg+xml")}, headers=ORIGIN
        ).status_code
        == 415
    )
    assert (
        studio.api.client.post(
            path,
            files={"file": ("large.wav", b"x" * (5 * 1024**2 + 1), "audio/wav")},
            headers=ORIGIN,
        ).status_code
        == 413
    )
    login("other")
    assert (
        studio.api.client.post(
            path, files={"file": ("music.wav", valid, "audio/wav")}, headers=ORIGIN
        ).status_code
        == 404
    )
    login("viewer")
    assert (
        studio.api.client.post(
            path, files={"file": ("music.wav", valid, "audio/wav")}, headers=ORIGIN
        ).status_code
        == 403
    )


def test_narration_probe_must_confirm_an_audio_stream(production):
    studio = production
    render_id = identifier(studio.create())
    studio.prepare(
        render_id, probe=lambda _: SimpleNamespace(duration_seconds=1.2, has_audio=False)
    )
    row = job(studio, render_id)
    assert row.status == "failed" and row.error_code == "narration_audio_rejected"
    assert row.output_asset_id is None
    with studio.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(NarrationAsset)) == 0


@pytest.mark.integration
def test_concurrent_postgres_workers_claim_a_render_once():
    """Real transaction isolation, confined to an ephemeral test schema."""
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.config import Settings
    from app.db import make_engine, make_session_factory
    from app.models import Base, User
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = os.environ.get("VIDEO_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set isolated PostgreSQL test database URL")
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg" or not (
        parsed.database == "video_studio_test"
        or (parsed.database or "").startswith("video_studio_test_")
    ):
        pytest.fail("Refusing render claim tests outside a dedicated video_studio_test database")
    schema = "video_render_claim_" + uuid4().hex
    admin = make_engine(url)
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=4,
            max_overflow=0,
            connect_args={
                "connect_timeout": 5,
                "options": f"-c search_path={schema} -c statement_timeout=15000",
            },
        )
        Base.metadata.create_all(engine)
        sessions = make_session_factory(engine)
        with sessions() as db:
            user = User(
                email="render-claims@example.com", password_hash="offline-only", role="editor"
            )
            db.add(user)
            db.flush()
            project = Project(owner_id=user.id, name="Concurrent local render")
            db.add(project)
            db.flush()
            row = RenderingJob(
                project_id=project.id,
                requested_by_id=user.id,
                idempotency_key=str(uuid4()),
                status="pending",
                stage="narration",
                composition_snapshot={"settings": {"narration_enabled": False}},
            )
            db.add(row)
            db.commit()
            render_id = row.id
        settings = Settings(
            _env_file=None,
            environment="test",
            database_url="sqlite://",
            redis_url="redis://localhost:6379/0",
            session_secret="offline-unit-session-" * 3,
            render_enabled=True,
        )
        barrier = threading.Barrier(2)

        def run_claim(_):
            barrier.wait(timeout=10)
            return claim(sessions, settings, render_id, "narration", lambda _: True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run_claim, range(2)))
        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        with sessions() as db:
            row = db.get(RenderingJob, render_id)
            assert row.status == "admitted" and row.attempt_count == 1
            assert row.lease_token == winners[0][0]
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("revocation", ["disabled", "demoted", "ownership_changed"])
def test_requester_revocation_stops_later_scene_speech_calls(production, revocation):
    from app.models import User

    studio = production
    render_id = identifier(studio.create())
    calls = []

    class RevokingTTS:
        async def synthesize(self, text, *_args, **_kwargs):
            calls.append(text)
            with studio.api.sessions() as db:
                if revocation == "ownership_changed":
                    db.get(Project, studio.project_id).owner_id = studio.api.user_ids["other"]
                else:
                    requester = db.get(User, studio.api.user_ids["editor"])
                    if revocation == "disabled":
                        requester.active = False
                    else:
                        requester.role = "viewer"
                db.commit()
            return SpeechResult(audio=b"ID3mock", boundaries=[SpeechBoundary(0.1, 1.1, text)])

        async def aclose(self):
            pass

    studio.prepare(render_id, tts_factory=lambda _: RevokingTTS())
    row = job(studio, render_id)
    assert row.status in {"paused", "failed"} and row.stage == "narration"
    assert len(calls) == 1
    with studio.api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(NarrationAsset)) == 0
