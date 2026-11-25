"""Recoverable local media jobs; these tasks never call paid video/director APIs."""

import asyncio
import fcntl
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from sqlalchemy import or_, select, update

from app.generation import GenerationError, fingerprint, safe_code
from app.models import (
    AuditEvent,
    MediaAsset,
    NarrationAsset,
    Project,
    RenderingJob,
    SubtitleAsset,
    User,
)
from app.production import dispatch_render, render_gate, source_binding
from app.production_storage import S3Archive, artifact_key, artifact_path, digest_file, publish_file
from app.security import aware_utc
from app.storage import _storage_root, local_asset_path


class ProductionError(Exception):
    def __init__(self, code, *, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


def _runtime(settings=None, sessions=None, dispatcher=None):
    from app.config import get_settings
    from app.db import make_engine, make_session_factory
    from app.runtime import dispatch_production

    settings = settings or get_settings()
    engine = None
    if sessions is None:
        engine = make_engine(settings.database_url)
        sessions = make_session_factory(engine)
    return settings, sessions, dispatcher or dispatch_production, engine


def locked_job(db, identifier):
    project_id = db.scalar(select(RenderingJob.project_id).where(RenderingJob.id == identifier))
    if project_id is None:
        return None
    db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    return db.scalar(
        select(RenderingJob)
        .where(RenderingJob.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def claim(sessions, settings, identifier, stage, admission):
    with sessions() as db:
        job = locked_job(db, identifier)
        if not job or job.status not in {"pending", "paused"} or job.stage != stage:
            return None
        if job.next_attempt_at and aware_utc(job.next_attempt_at) > datetime.now(UTC):
            return None
        options = job.composition_snapshot.get("settings", {})
        try:
            render_gate(
                settings, narration=stage == "narration" and options.get("narration_enabled", False)
            )
            requester = db.get(User, job.requested_by_id)
            if not requester or not requester.active or requester.role not in {"editor", "admin"}:
                raise GenerationError("render_requester_unavailable")
            try:
                admitted = admission(settings) is True
            except Exception:
                admitted = False
            if not admitted:
                raise ProductionError("host_admission_refused", retryable=True)
        except (GenerationError, ProductionError) as exc:
            job.status = "paused"
            job.error_code = exc.code
            job.dispatch_state = "pending"
            job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=30)
            db.commit()
            return None
        if job.attempt_count >= settings.render_max_attempts:
            job.status = "failed"
            job.error_code = "render_attempt_limit"
            db.commit()
            return None
        token = uuid4().hex
        job.lease_token = token
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=settings.render_lease_seconds)
        job.status = "admitted" if stage == "narration" else "running"
        job.started_at = job.started_at or datetime.now(UTC)
        job.attempt_count += 1
        job.dispatch_state = "not_needed"
        job.error_code = None
        db.commit()
        return token, dict(job.composition_snapshot), job.project_id


class Lease:
    def __init__(self, sessions, settings, identifier, token, admission):
        self.sessions, self.settings, self.identifier = sessions, settings, identifier
        self.token, self.admission = token, admission
        self.stop = threading.Event()
        self.allowed = True
        self.last_check = 0.0
        self.thread = None
        self.lock = threading.Lock()

    def check(self, *, force=False):
        with self.lock:
            if not force and time.monotonic() - self.last_check < 3:
                return self.allowed
            self.last_check = time.monotonic()
            try:
                with self.sessions() as db:
                    job = db.get(RenderingJob, self.identifier)
                    requester = db.get(User, job.requested_by_id) if job else None
                    project = db.get(Project, job.project_id) if job else None
                    authorized = bool(
                        requester
                        and requester.active
                        and requester.role in {"editor", "admin"}
                        and project
                        and (requester.role == "admin" or project.owner_id == requester.id)
                    )
                    result = db.execute(
                        update(RenderingJob)
                        .where(
                            RenderingJob.id == self.identifier,
                            RenderingJob.lease_token == self.token,
                            RenderingJob.status.in_(["admitted", "running"]),
                        )
                        .values(
                            lease_expires_at=datetime.now(UTC)
                            + timedelta(seconds=self.settings.render_lease_seconds)
                        )
                    )
                    self.allowed = (
                        authorized
                        and result.rowcount == 1
                        and self.admission(self.settings) is True
                    )
                    db.commit()
            except Exception:
                self.allowed = False
            return self.allowed

    def progress(self, percentage):
        if not self.check():
            raise ProductionError("production_admission_lost", retryable=True)
        with self.sessions() as db:
            db.execute(
                update(RenderingJob)
                .where(
                    RenderingJob.id == self.identifier,
                    RenderingJob.lease_token == self.token,
                    RenderingJob.status.in_(["admitted", "running"]),
                )
                .values(progress_percent=max(0, min(99, int(percentage))))
            )
            db.commit()

    def __enter__(self):
        def heartbeat():
            while not self.stop.wait(20):
                self.check(force=True)

        self.thread = threading.Thread(target=heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=2)


@contextmanager
def media_lock(settings, name):
    import os
    import stat

    descriptor = os.open(
        _storage_root(settings) / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "a+b") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProductionError("unsafe_media_lock")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProductionError("media_worker_busy", retryable=True) from None
        yield


@contextmanager
def workdir(settings, identifier, stage):
    root = Path(settings.temp_dir).absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.resolve() != root:
        raise ProductionError("unsafe_scratch_root")
    folder = root / f"{stage}-{UUID(str(identifier))}"
    if folder.is_symlink():
        raise ProductionError("unsafe_scratch_path")
    if folder.exists():
        shutil.rmtree(folder)  # exact server-generated path, after worker lock/lease
    folder.mkdir(mode=0o700)
    try:
        yield folder
    finally:
        if folder.exists() and not folder.is_symlink():
            shutil.rmtree(folder)


def checked_asset(db, project_id, identifier, sha256, settings, kinds):
    asset = db.get(MediaAsset, UUID(str(identifier)))
    if (
        not asset
        or asset.project_id != project_id
        or asset.status != "ready"
        or asset.kind not in kinds
        or asset.storage_backend != "local"
        or asset.sha256 != sha256
        or not asset.object_key.startswith(str(project_id) + "/")
    ):
        raise ProductionError("render_input_changed")
    if asset.kind == "clip":
        path = local_asset_path(settings, asset.object_key)
    elif asset.kind in {"reference_image", "logo"}:
        from app.reference_storage import reference_path

        path = reference_path(settings, asset.object_key)
    else:
        path = artifact_path(settings, asset.object_key)
    if digest_file(path, settings.max_media_bytes) != (asset.size_bytes, sha256):
        raise ProductionError("render_input_changed")
    return asset, path


def store_generated(
    sessions,
    settings,
    project_id,
    identifier,
    source,
    kind,
    mime,
    *,
    scene_version_id=None,
    metadata=None,
):
    source = Path(source)
    size, digest = digest_file(source, settings.max_media_bytes)
    extension = source.suffix.lstrip(".")
    key = artifact_key(project_id, identifier, extension)
    with sessions() as db:
        asset = db.get(MediaAsset, identifier)
        if asset:
            if asset.project_id != project_id or asset.object_key != key or asset.kind != kind:
                raise ProductionError("artifact_publication_conflict")
            if asset.sha256 != digest:
                path = artifact_path(settings, key, must_exist=False)
                if asset.status != "pending" or path.exists():
                    raise ProductionError("artifact_publication_conflict")
                # An intent whose bytes were never published may be replaced
                # under the exclusive worker lock, without touching ready media.
                asset.sha256, asset.size_bytes = digest, size
                asset.metadata_json = metadata or {}
                db.commit()
        else:
            asset = MediaAsset(
                id=identifier,
                project_id=project_id,
                kind=kind,
                object_key=key,
                mime_type=mime,
                size_bytes=size,
                sha256=digest,
                status="pending",
                scene_version_id=scene_version_id,
                metadata_json=metadata or {},
            )
            db.add(asset)
            db.commit()  # Pending intent survives uncertain publication/commit.
    publish_file(settings, source, project_id, identifier, extension, settings.max_media_bytes)
    with sessions() as db:
        asset = db.get(MediaAsset, identifier)
        asset.status = "ready"
        db.commit()
    return identifier


def recover_artifact(db, settings, project_id, identifier, kind):
    asset = db.get(MediaAsset, identifier)
    if asset is None:
        return None
    if (
        asset.project_id != project_id
        or asset.kind != kind
        or asset.status not in {"pending", "ready"}
    ):
        raise ProductionError("artifact_publication_conflict")
    path = artifact_path(settings, asset.object_key, must_exist=False)
    if not path.exists():
        if asset.status == "ready":
            raise ProductionError("render_input_changed")
        return None
    if digest_file(path, settings.max_media_bytes) != (asset.size_bytes, asset.sha256):
        raise ProductionError("render_input_changed")
    asset.status = "ready"
    db.commit()
    return asset


def fail_job(sessions, settings, identifier, token, exc):
    with sessions() as db:
        job = locked_job(db, identifier)
        if not job or job.lease_token != token or job.status == "cancelled":
            return
        code = safe_code(getattr(exc, "code", None), "production_failed")
        retryable = bool(getattr(exc, "retryable", False))
        job.status = (
            "paused" if retryable and job.attempt_count < settings.render_max_attempts else "failed"
        )
        job.error_code = code
        job.lease_token = None
        job.lease_expires_at = None
        job.dispatch_state = "pending" if job.status == "paused" else "not_needed"
        job.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=min(300, 15 * 2**job.attempt_count)
        )
        if job.status == "failed":
            job.completed_at = datetime.now(UTC)
        db.commit()


async def _prepare(identifier, *, sessions, settings, dispatcher, tts_factory, admission, probe):
    from app.tts import SpeechBoundary, SubtitleScene, build_subtitles, to_srt, validate_timing

    claimed = claim(sessions, settings, identifier, "narration", admission)
    if not claimed:
        return
    token, snapshot, project_id = claimed
    options, prepared, subtitle_scenes = snapshot["settings"], {}, []
    tts = None
    try:
        with (
            Lease(sessions, settings, identifier, token, admission) as lease,
            media_lock(settings, ".narration.lock"),
            workdir(settings, identifier, "narration") as folder,
        ):
            for index, scene in enumerate(snapshot["scenes"]):
                lease.progress(5 + int(25 * index / len(snapshot["scenes"])))
                boundaries, audio_duration = [], None
                narration = scene["narration"].strip() if options["narration_enabled"] else ""
                if narration:
                    fp = fingerprint(
                        {
                            "contract": "edge-word-v1",
                            "version": scene["scene_version_id"],
                            "text": narration,
                            "voice": options["voice"],
                            "rate": options["speaking_rate"],
                            "language": options["language"],
                        }
                    )
                    asset_id = uuid5(project_id, "narration:" + fp)
                    with sessions() as db:
                        cached = db.scalar(
                            select(NarrationAsset).where(NarrationAsset.fingerprint == fp)
                        )
                        if cached:
                            asset = recover_artifact(
                                db, settings, project_id, cached.media_asset_id, "narration"
                            )
                            if asset:
                                checked_asset(
                                    db, project_id, asset.id, asset.sha256, settings, {"narration"}
                                )
                                boundaries = [
                                    SpeechBoundary(**value) for value in cached.alignment_data
                                ]
                                audio_duration = float(cached.duration_seconds)
                                asset_id = asset.id
                            else:
                                raise ProductionError("render_input_changed")
                        else:
                            asset = recover_artifact(
                                db, settings, project_id, asset_id, "narration"
                            )
                            if asset:
                                metadata = asset.metadata_json or {}
                                if metadata.get("fingerprint") != fp:
                                    raise ProductionError("narration_cache_changed")
                                boundaries = [
                                    SpeechBoundary(**value) for value in metadata["alignment_data"]
                                ]
                                audio_duration = float(metadata["duration_seconds"])
                                validate_timing(
                                    boundaries, scene["duration_seconds"], audio_duration
                                )
                                db.add(
                                    NarrationAsset(
                                        scene_version_id=UUID(scene["scene_version_id"]),
                                        media_asset_id=asset_id,
                                        provider="edge",
                                        voice=options["voice"],
                                        language=options["language"],
                                        duration_seconds=audio_duration,
                                        speaking_rate=options["speaking_rate"],
                                        fingerprint=fp,
                                        alignment_data=metadata["alignment_data"],
                                    )
                                )
                                db.commit()
                    if audio_duration is None:
                        if tts is None:
                            tts = tts_factory(settings)
                        result = await tts.synthesize(
                            narration,
                            options["voice"],
                            float(options["speaking_rate"]),
                            options["language"],
                            approved=True,
                        )
                        if result.format != "mp3" or not 1 <= len(result.audio) <= 10 * 1024**2:
                            raise ProductionError("narration_audio_rejected")
                        if not lease.check(force=True):
                            raise ProductionError("production_admission_lost", retryable=True)
                        audio = folder / "speech.mp3"
                        audio.write_bytes(result.audio)
                        info = probe(audio)
                        if not info.has_audio:
                            raise ProductionError("narration_audio_rejected")
                        audio_duration, boundaries = info.duration_seconds, result.boundaries
                        validate_timing(boundaries, scene["duration_seconds"], audio_duration)
                        if not lease.check(force=True):
                            raise ProductionError("production_admission_lost", retryable=True)
                        store_generated(
                            sessions,
                            settings,
                            project_id,
                            asset_id,
                            audio,
                            "narration",
                            "audio/mpeg",
                            scene_version_id=UUID(scene["scene_version_id"]),
                            metadata={
                                "fingerprint": fp,
                                "duration_seconds": audio_duration,
                                "alignment_data": [asdict(b) for b in boundaries],
                            },
                        )
                        with sessions() as db:
                            if not db.scalar(
                                select(NarrationAsset.id).where(NarrationAsset.fingerprint == fp)
                            ):
                                db.add(
                                    NarrationAsset(
                                        scene_version_id=UUID(scene["scene_version_id"]),
                                        media_asset_id=asset_id,
                                        provider="edge",
                                        voice=options["voice"],
                                        language=options["language"],
                                        duration_seconds=audio_duration,
                                        speaking_rate=options["speaking_rate"],
                                        fingerprint=fp,
                                        alignment_data=[asdict(b) for b in boundaries],
                                    )
                                )
                                db.commit()
                        audio.unlink()
                    validate_timing(boundaries, scene["duration_seconds"], audio_duration)
                    prepared[scene["scene_id"]] = str(asset_id)
                subtitle_scenes.append(
                    SubtitleScene(boundaries, scene["duration_seconds"], audio_duration)
                )
            subtitle_id = None
            if options["narration_enabled"] and any(s.boundaries for s in subtitle_scenes):
                cues = build_subtitles(
                    subtitle_scenes,
                    opening_offset_seconds=options["title_duration_seconds"]
                    if options["opening_title"]
                    else 0,
                )
                path = folder / "captions.srt"
                path.write_text(to_srt(cues), encoding="utf-8")
                if not lease.check(force=True):
                    raise ProductionError("production_admission_lost", retryable=True)
                subtitle_id = uuid5(identifier, "subtitles")
                store_generated(
                    sessions,
                    settings,
                    project_id,
                    subtitle_id,
                    path,
                    "subtitles",
                    "application/x-subrip",
                )
                with sessions() as db:
                    if not db.scalar(
                        select(SubtitleAsset.id).where(SubtitleAsset.media_asset_id == subtitle_id)
                    ):
                        db.add(
                            SubtitleAsset(
                                project_id=project_id,
                                media_asset_id=subtitle_id,
                                language=options["language"],
                                alignment_source="speech_boundaries",
                            )
                        )
                        db.commit()
            if not lease.check(force=True):
                raise ProductionError("production_admission_lost", retryable=True)
            with sessions() as db:
                job = locked_job(db, identifier)
                if job.lease_token != token or job.status == "cancelled":
                    return
                job.composition_snapshot = {
                    **snapshot,
                    "prepared": {
                        "narration": prepared,
                        "subtitle_asset_id": str(subtitle_id) if subtitle_id else None,
                    },
                }
                job.subtitle_asset_id = subtitle_id
                job.stage, job.status = "rendering", "pending"
                job.progress_percent, job.attempt_count = 35, 0
                job.lease_token = job.lease_expires_at = None
                job.next_attempt_at = None
                job.dispatch_state = "pending"
                db.commit()
                dispatch_render(db, identifier, settings, dispatcher)
    except Exception as exc:
        fail_job(sessions, settings, identifier, token, exc)
    finally:
        if tts is not None:
            await tts.aclose()


def prepare_render(
    identifier,
    *,
    sessions=None,
    settings=None,
    dispatcher=None,
    tts_factory=None,
    admission=None,
    probe=None,
):
    from app.host_guard import require_admission
    from app.media import probe_media
    from app.runtime import make_tts

    settings, sessions, dispatcher, engine = _runtime(settings, sessions, dispatcher)
    try:
        return asyncio.run(
            _prepare(
                UUID(str(identifier)),
                sessions=sessions,
                settings=settings,
                dispatcher=dispatcher,
                tts_factory=tts_factory or make_tts,
                admission=admission or require_admission,
                probe=probe or probe_media,
            )
        )
    finally:
        if engine:
            engine.dispose()


def render_video(
    identifier,
    *,
    sessions=None,
    settings=None,
    dispatcher=None,
    admission=None,
    composer=None,
    archive_factory=None,
):
    from app.host_guard import require_admission
    from app.media import CompositionRequest, SceneMedia, compose

    settings, sessions, dispatcher, engine = _runtime(settings, sessions, dispatcher)
    identifier, admission = UUID(str(identifier)), admission or require_admission
    claimed = None
    try:
        claimed = claim(sessions, settings, identifier, "rendering", admission)
        if not claimed:
            return
        token, snapshot, project_id = claimed
        options, prepared = snapshot["settings"], snapshot["prepared"]
        with (
            Lease(sessions, settings, identifier, token, admission) as lease,
            media_lock(settings, ".render.lock"),
            workdir(settings, identifier, "render") as folder,
        ):
            scene_media = []
            with sessions() as db:
                for scene in snapshot["scenes"]:
                    _asset, clip = checked_asset(
                        db,
                        project_id,
                        scene["clip_asset_id"],
                        scene["clip_sha256"],
                        settings,
                        {"clip"},
                    )
                    narration = None
                    if scene["scene_id"] in prepared.get("narration", {}):
                        asset = db.get(MediaAsset, UUID(prepared["narration"][scene["scene_id"]]))
                        _asset, narration = checked_asset(
                            db, project_id, asset.id, asset.sha256, settings, {"narration"}
                        )
                    scene_media.append(
                        SceneMedia(
                            path=clip,
                            duration_seconds=scene["duration_seconds"],
                            transition=scene["transition"],
                            narration_path=narration,
                        )
                    )
                attachments = {a["id"]: a["sha256"] for a in snapshot["source"]["attachments"]}
                paths = {}
                for key, kinds in (
                    ("music_asset_id", {"music"}),
                    ("watermark_asset_id", {"reference_image", "logo"}),
                ):
                    if options[key]:
                        _asset, paths[key] = checked_asset(
                            db, project_id, options[key], attachments[options[key]], settings, kinds
                        )
                subtitles = None
                if prepared.get("subtitle_asset_id"):
                    asset = db.get(MediaAsset, UUID(prepared["subtitle_asset_id"]))
                    _asset, subtitles = checked_asset(
                        db, project_id, asset.id, asset.sha256, settings, {"subtitles"}
                    )
            output_id = uuid5(identifier, "final-video")
            with sessions() as db:
                cached = recover_artifact(db, settings, project_id, output_id, "final_video")
                if cached:
                    checked_asset(
                        db, project_id, output_id, cached.sha256, settings, {"final_video"}
                    )
                    report, warnings = (
                        cached.metadata_json["validation_report"],
                        cached.metadata_json["warnings"],
                    )
            if not cached:
                request = CompositionRequest(
                    scenes=scene_media,
                    output=folder / "final.mp4",
                    scratch=folder,
                    aspect_ratio=options["aspect_ratio"],
                    subtitles_path=subtitles,
                    burn_subtitles=options["burn_subtitles"] and subtitles is not None,
                    music_path=paths.get("music_asset_id"),
                    watermark_path=paths.get("watermark_asset_id"),
                    opening_title=options["opening_title"],
                    closing_title=options["closing_title"],
                    title_duration_seconds=options["title_duration_seconds"],
                    allow_upscale=options["allow_upscale"],
                    music_volume=options["music_volume"],
                    source_audio_volume=options["source_audio_volume"],
                    timeout_seconds=settings.render_timeout_seconds,
                    max_output_bytes=settings.max_media_bytes,
                    max_scratch_bytes=settings.render_scratch_max_bytes,
                    ffmpeg_threads=1,
                    progress=lambda n: lease.progress(35 + int(n * 0.6)),
                    admission=lease.check,
                )
                result = (composer or compose)(request)
                report, warnings = result.report, result.warnings
                if not lease.check(force=True):
                    raise ProductionError("production_admission_lost", retryable=True)
                store_generated(
                    sessions,
                    settings,
                    project_id,
                    output_id,
                    result.path,
                    "final_video",
                    "video/mp4",
                    metadata={
                        "render_job_id": str(identifier),
                        "validation_report": report,
                        "warnings": warnings,
                        "width": result.width,
                        "height": result.height,
                    },
                )
            if not lease.check(force=True):
                raise ProductionError("production_admission_lost", retryable=True)
            archive = (archive_factory or S3Archive)(settings)
            for asset_id in [
                output_id,
                *(
                    [UUID(prepared["subtitle_asset_id"])]
                    if prepared.get("subtitle_asset_id")
                    else []
                ),
            ]:
                with sessions() as db:
                    asset = db.get(MediaAsset, asset_id)
                    values = (asset.object_key, asset.mime_type, asset.sha256, asset.size_bytes)
                path = artifact_path(settings, values[0])
                if not lease.check(force=True):
                    raise ProductionError("production_admission_lost", retryable=True)
                # Never hold a database transaction across object-storage I/O.
                archive_result = archive.archive(path, *values)
                with sessions() as db:
                    asset = db.get(MediaAsset, asset_id)
                    asset.metadata_json = {**asset.metadata_json, "archive": archive_result}
                    db.commit()
            with sessions() as db:
                job = locked_job(db, identifier)
                if job.lease_token != token or job.status == "cancelled":
                    return
                project = db.get(Project, project_id)
                requester = db.get(User, job.requested_by_id)
                if (
                    not requester
                    or not requester.active
                    or requester.role not in {"editor", "admin"}
                    or (requester.role != "admin" and project.owner_id != requester.id)
                ):
                    raise ProductionError("render_requester_unavailable")
                job.status, job.stage, job.progress_percent = "succeeded", "completed", 100
                job.output_asset_id, job.warnings, job.validation_report = (
                    output_id,
                    warnings,
                    report,
                )
                job.completed_at = datetime.now(UTC)
                job.lease_token = job.lease_expires_at = None
                job.error_code = None
                if snapshot["binding_hash"] == fingerprint(source_binding(db, project)):
                    project.status = "completed"
                db.add(
                    AuditEvent(
                        project_id=project_id,
                        actor_user_id=job.requested_by_id,
                        event_type="render.completed",
                        entity_type="rendering_job",
                        entity_id=identifier,
                        details={},
                    )
                )
                db.commit()
    except Exception as exc:
        if claimed:
            fail_job(sessions, settings, identifier, claimed[0], exc)
    finally:
        if engine:
            engine.dispose()


def reconcile_production(*, sessions=None, settings=None, dispatcher=None):
    settings, sessions, dispatcher, engine = _runtime(settings, sessions, dispatcher)
    try:
        now = datetime.now(UTC)
        with sessions() as db:
            ids = db.scalars(
                select(RenderingJob.id)
                .where(
                    RenderingJob.status.in_(["admitted", "running"]),
                    or_(
                        RenderingJob.lease_expires_at.is_(None),
                        RenderingJob.lease_expires_at <= now,
                    ),
                )
                .order_by(RenderingJob.updated_at)
                .limit(100)
            ).all()
        for identifier in ids:
            with sessions() as db:
                job = locked_job(db, identifier)
                if job.status not in {"admitted", "running"} or (
                    job.lease_expires_at and aware_utc(job.lease_expires_at) > now
                ):
                    continue
                job.status = (
                    "paused" if job.attempt_count < settings.render_max_attempts else "failed"
                )
                job.error_code = "worker_interrupted"
                job.lease_token = job.lease_expires_at = None
                job.next_attempt_at = now
                job.dispatch_state = "pending"
                db.commit()
        if not settings.render_enabled:
            return
        with sessions() as db:
            query = select(RenderingJob.id)
            if not settings.narration_enabled:
                # A disabled external speech queue must not starve already
                # prepared local renders during broker recovery.
                query = query.where(
                    or_(
                        RenderingJob.stage != "narration",
                        RenderingJob.composition_snapshot["settings"]["narration_enabled"]
                        .as_boolean()
                        .is_(False),
                    )
                )
            ids = db.scalars(
                query.where(
                    RenderingJob.status.in_(["pending", "paused"]),
                    or_(
                        RenderingJob.next_attempt_at.is_(None), RenderingJob.next_attempt_at <= now
                    ),
                    or_(
                        RenderingJob.dispatch_state.in_(["pending", "error"]),
                        RenderingJob.updated_at
                        < now - timedelta(seconds=settings.render_lease_seconds),
                    ),
                )
                .order_by(RenderingJob.updated_at, RenderingJob.id)
                .limit(100)
            ).all()
        for identifier in ids:
            with sessions() as db:
                try:
                    dispatch_render(db, identifier, settings, dispatcher)
                except GenerationError:
                    pass
    finally:
        if engine:
            engine.dispose()
