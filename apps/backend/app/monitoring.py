"""Read-only, bounded Studio metrics; persisted queue states are not Redis depth."""

import os
import stat
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from app.admission import evaluate
from app.models import ActualUsageCost, DirectorJob, GenerationJob, RenderingJob, WorkerHeartbeat


def storage_evidence(config, *, max_entries=20000, max_seconds=0.25):
    result = {
        "available": False,
        "used_bytes": None,
        "free_bytes": None,
        "max_bytes": config.storage_max_bytes,
        "min_free_bytes": config.storage_min_free_bytes,
    }
    # Do not create a volume or follow links while answering a monitoring read.
    try:
        if config.storage_dir.resolve() != config.storage_dir.absolute() or not stat.S_ISDIR(
            config.storage_dir.lstat().st_mode
        ):
            return result
        space = os.statvfs(config.storage_dir)
        result["free_bytes"] = space.f_bavail * space.f_frsize
        pending, used, entries = [config.storage_dir], 0, 0
        deadline = time.monotonic() + max_seconds
        while pending:
            with os.scandir(pending.pop()) as directory:
                for entry in directory:
                    entries += 1
                    if entries > max_entries or time.monotonic() > deadline:
                        return result
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_nlink != 1:
                            return result
                        used += info.st_size
                    else:
                        return result
        result.update(available=True, used_bytes=used)
    except OSError:
        pass
    return result


def operations(db, config, *, now=None):
    now = now or datetime.now(UTC)
    allowed, reason = evaluate(str(config.host_signal_path), now=now)
    storage = storage_evidence(config)
    alerts, queues = [], []

    def alert(code, message, count=1, severity="warning"):
        alerts.append({"code": code, "severity": severity, "count": count, "message": message})

    if config.render_enabled and not allowed:
        alert("render_admission_blocked", "New renders are paused by the host admission guard.")
    if not storage["available"]:
        alert(
            "storage_unavailable",
            "Studio volume usage could not be verified within the scan limit.",
        )
    elif (
        storage["used_bytes"] >= config.storage_max_bytes * 0.9
        or storage["free_bytes"] < config.storage_min_free_bytes
    ):
        alert("storage_pressure", "Studio storage is near its quota or free-space floor.")
    for name in ("generation", "narration", "rendering"):
        models = (GenerationJob, DirectorJob) if name == "generation" else (RenderingJob,)
        queued = running = failed = 0
        oldest = None
        for model in models:
            where = (
                []
                if name == "generation"
                else [RenderingJob.stage == ("narration" if name == "narration" else "rendering")]
            )
            queued_states = (
                ("pending", "approved", "submitted", "queued", "cancellation_requested")
                if name == "generation"
                else ("pending", "paused", "admitted")
            )
            running_states = ("submitting", "running") if name == "generation" else ("running",)
            queued += db.scalar(
                select(func.count())
                .select_from(model)
                .where(*where, model.status.in_(queued_states))
            )
            running += db.scalar(
                select(func.count())
                .select_from(model)
                .where(*where, model.status.in_(running_states))
            )
            failed += db.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    *where, model.status == "failed", model.updated_at >= now - timedelta(hours=24)
                )
            )
            value = db.scalar(
                select(func.min(model.created_at)).where(*where, model.status.in_(queued_states))
            )
            if value is not None and (oldest is None or value < oldest):
                oldest = value
        workers = db.scalar(
            select(func.count())
            .select_from(WorkerHeartbeat)
            .where(
                WorkerHeartbeat.queue == name,
                WorkerHeartbeat.last_seen_at >= now - timedelta(seconds=90),
                WorkerHeartbeat.last_seen_at <= now + timedelta(seconds=5),
            )
        )
        enabled = (
            (config.paid_generation_enabled or config.director_enabled)
            if name == "generation"
            else (config.narration_enabled if name == "narration" else config.render_enabled)
        )
        queues.append(
            {
                "name": name,
                "queued": queued,
                "running": running,
                "failed": failed,
                "oldest_pending_at": oldest,
                "worker_count": workers,
                "enabled": enabled,
                "basis": "persisted_job_states",
            }
        )
        if failed:
            alert(
                f"{name}_failures", f"{name.capitalize()} jobs failed in the last 24 hours.", failed
            )
        if enabled and workers == 0:
            alert(f"{name}_worker_missing", f"No recent {name} worker heartbeat was recorded.")
    unresolved = sum(
        db.scalar(
            select(func.count()).select_from(model).where(model.status == "reconciliation_required")
        )
        for model in (GenerationJob, DirectorJob)
    )
    unknown = db.scalar(
        select(func.count())
        .select_from(ActualUsageCost)
        .where(or_(ActualUsageCost.status != "confirmed", ActualUsageCost.amount_usd.is_(None)))
    )
    if unresolved:
        alert(
            "paid_reconciliation_required",
            "Verify ambiguous paid requests before taking further action.",
            unresolved,
            "error",
        )
    if unknown:
        alert(
            "unconfirmed_charges",
            "Some provider charges are unavailable or pending verification.",
            unknown,
        )
    expired = db.scalar(
        select(func.count())
        .select_from(RenderingJob)
        .where(RenderingJob.status == "running", RenderingJob.lease_expires_at < now)
    )
    if expired:
        alert("expired_render_leases", "Interrupted local renders are awaiting recovery.", expired)
    return {
        "status": "attention" if alerts else "healthy",
        "checked_at": now,
        "admission": {"allowed": allowed, "reason": reason},
        "storage": storage,
        "queues": queues,
        "alerts": alerts,
        "unresolved_paid_jobs": unresolved,
        "unknown_cost_count": unknown,
    }
