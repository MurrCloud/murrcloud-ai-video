"""Throttled worker presence. Failure logs contain no SQL, URLs or credentials."""

import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.models import WorkerHeartbeat

logger = logging.getLogger("video.monitoring")
_lock = threading.Lock()
_last_seen = 0.0
_engine = None
_engine_pid = None


def record_heartbeat(db, worker_id, queue, *, now=None):
    if (
        queue not in {"generation", "narration", "rendering"}
        or not isinstance(worker_id, str)
        or not 1 <= len(worker_id) <= 128
    ):
        return False
    now = now or datetime.now(UTC)
    row = db.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id))
    if row is None:
        db.add(WorkerHeartbeat(worker_id=worker_id, queue=queue, last_seen_at=now))
    else:
        row.queue, row.last_seen_at = queue, now
    db.execute(
        delete(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at < now - timedelta(days=7))
    )
    db.commit()
    return True


def worker_presence(sender=None, **_kwargs):
    global _engine, _engine_pid, _last_seen
    queue = os.environ.get("VIDEO_WORKER_QUEUE", "")
    worker_id = getattr(sender, "hostname", None) or getattr(
        getattr(sender, "eventer", None), "hostname", None
    )
    if queue not in {"generation", "narration", "rendering"} or not worker_id:
        return
    if not _lock.acquire(blocking=False):
        return
    try:
        tick = time.monotonic()
        if _engine_pid == os.getpid() and tick - _last_seen < 30:
            return
        _last_seen = tick
        from app.config import get_settings

        config = get_settings()
        if _engine is None or _engine_pid != os.getpid():
            if _engine is not None:
                _engine.dispose(close=False)
            # A small independent instrumentation pool never consumes task pool
            # capacity. Bound connect/statement/lock waits to two seconds.
            _engine = create_engine(
                config.database_url,
                pool_size=1,
                max_overflow=0,
                pool_timeout=1,
                pool_pre_ping=True,
                connect_args={
                    "connect_timeout": 2,
                    "options": "-c statement_timeout=2000 -c lock_timeout=1000",
                },
            )
            _engine_pid = os.getpid()
        with Session(_engine) as db:
            record_heartbeat(db, worker_id, queue)
    except Exception:
        logger.warning("Worker heartbeat could not be recorded")
    finally:
        _lock.release()


def install_worker_signals():
    from celery.signals import heartbeat_sent, worker_ready

    worker_ready.connect(worker_presence, weak=False)
    heartbeat_sent.connect(worker_presence, weak=False)
