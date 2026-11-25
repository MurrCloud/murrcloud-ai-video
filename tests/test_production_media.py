"""Real synthetic clips/audio through preparation, composition and private downloads.

The speech adapter supplies synthetic tones and known word-boundary fixtures;
this verifies synchronization and encoding, not live voice quality or cloud video.
"""

import hashlib
import shutil
import subprocess
from uuid import UUID

import pytest
from app.media import probe_media
from app.models import MediaAsset, Project, ProjectSettings, RenderingJob, Scene
from app.production_storage import artifact_path
from app.production_tasks import prepare_render, render_video
from app.tts import SpeechBoundary, SpeechResult
from sqlalchemy import select
from test_production_jobs import production  # noqa: F401


def test_real_synthetic_chinese_production_and_range_preview(production, tmp_path):  # noqa: F811
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/FFprobe are required; installed in the CI media image")
    p = production
    settings = p.settings.model_copy(update={"render_timeout_seconds": 180})
    api = p.api
    with api.sessions() as db:
        row = db.scalar(select(ProjectSettings).where(ProjectSettings.project_id == p.project_id))
        options = row.composition["production"]
        row.composition = {
            "production": {
                **options,
                "opening_title": "",
                "burn_subtitles": False,
                "language": "zh-CN",
                "voice": "zh-CN-XiaoxiaoNeural",
                "allow_upscale": True,
            }
        }
        for index, identifier in enumerate(p.scenes):
            scene = db.get(Scene, identifier)
            scene.narration = f"第{index + 1}幕中文旁白。"
        for index, identifier in enumerate(p.clip_ids):
            asset = db.get(MediaAsset, identifier)
            path = settings.storage_dir / asset.object_key
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c={'orange' if index == 0 else 'blue'}:s=320x180:r=24:d=5",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    str(path),
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
            assert result.returncode == 0
            data = path.read_bytes()
            asset.size_bytes, asset.sha256 = len(data), hashlib.sha256(data).hexdigest()
            asset.metadata_json = {"width": 320, "height": 180}
        db.commit()
    audio = tmp_path / "tone.mp3"
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1.2",
            "-c:a",
            "libmp3lame",
            "-threads",
            "1",
            str(audio),
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0
    calls = []

    class ToneTTS:
        async def synthesize(self, text, voice, rate, language, *, approved=False):
            assert approved and language == "zh-CN"
            calls.append(text)
            return SpeechResult(
                audio=audio.read_bytes(), boundaries=[SpeechBoundary(0.1, 1.0, text)]
            )

        async def aclose(self):
            pass

    created = p.create()
    assert created.status_code == 202, created.text
    identifier = UUID(created.json()["id"])
    prepare_render(
        identifier,
        sessions=api.sessions,
        settings=settings,
        dispatcher=lambda *_, **__: None,
        admission=lambda _: True,
        tts_factory=lambda _: ToneTTS(),
        probe=probe_media,
    )
    with api.sessions() as db:
        job = db.get(RenderingJob, identifier)
        assert job.stage == "rendering", job.error_code
    render_video(
        identifier,
        sessions=api.sessions,
        settings=settings,
        admission=lambda _: True,
        dispatcher=lambda *_, **__: None,
    )
    with api.sessions() as db:
        job = db.get(RenderingJob, identifier)
        assert job.status == "succeeded", job.error_code
        asset = db.get(MediaAsset, job.output_asset_id)
        info = probe_media(artifact_path(settings, asset.object_key))
        assert info.video_codec == "h264" and info.audio_codec == "aac"
        assert (info.width, info.height, info.fps) == (1920, 1080, 30)
        assert abs(info.duration_seconds - 10) < 0.15
        assert db.get(Project, p.project_id).status == "completed"
        final_id, subtitle_id = asset.id, job.subtitle_asset_id
        assert job.warnings
    assert len(calls) == 2
    preview = api.client.get(f"/api/v1/assets/{final_id}/preview", headers={"Range": "bytes=0-31"})
    assert preview.status_code == 206 and len(preview.content) == 32
    subtitles = api.client.get(f"/api/v1/assets/{subtitle_id}/download")
    assert subtitles.status_code == 200 and "中文旁白" in subtitles.text
    assert "00:00:00,100 --> 00:00:01,000" in subtitles.text
    assert "00:00:05,100 --> 00:00:06,000" in subtitles.text


def test_actor_revoked_at_composition_return_prevents_output_publication(production):  # noqa: F811
    from types import SimpleNamespace

    from app.models import User

    p = production
    identifier = UUID(p.create().json()["id"])
    p.prepare(identifier)

    def revoked(request):
        request.output.write_bytes(b"synthetic-not-published")
        with p.api.sessions() as db:
            db.get(User, p.api.user_ids["editor"]).active = False
            db.commit()
        return SimpleNamespace(
            path=request.output, width=1920, height=1080, report={"validated": True}, warnings=[]
        )

    p.render(identifier, composer=revoked)
    with p.api.sessions() as db:
        job = db.get(RenderingJob, identifier)
        assert job.status != "succeeded" and job.output_asset_id is None
        assert not db.scalar(select(MediaAsset).where(MediaAsset.kind == "final_video"))
    assert p.archives == []


def test_prepared_render_needs_no_enabled_network_speech_adapter(production):  # noqa: F811
    p = production
    identifier = UUID(p.create().json()["id"])
    p.prepare(identifier)
    p.settings.narration_enabled = False
    p.render(identifier)
    with p.api.sessions() as db:
        assert db.get(RenderingJob, identifier).status == "succeeded"
    assert len(p.speech_calls) == 2


def test_disabled_speech_backlog_does_not_starve_prepared_render_recovery(production):  # noqa: F811
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from app.production_tasks import reconcile_production

    p = production
    identifier = UUID(p.create().json()["id"])
    p.prepare(identifier)
    with p.api.sessions() as db:
        target = db.get(RenderingJob, identifier)
        target.dispatch_state = "pending"
        for _ in range(101):
            db.add(
                RenderingJob(
                    project_id=p.project_id,
                    requested_by_id=p.api.user_ids["editor"],
                    idempotency_key=str(uuid4()),
                    composition_snapshot=target.composition_snapshot,
                    stage="narration",
                    status="pending",
                    dispatch_state="pending",
                    updated_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        db.commit()
    p.settings.narration_enabled = False
    queued = []
    reconcile_production(
        sessions=p.api.sessions,
        settings=p.settings,
        dispatcher=lambda *args, **kwargs: queued.append(args),
    )
    assert queued == [("video.render_video", str(identifier))]
