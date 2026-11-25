"""Bounded generation queue. Paid requests require operator and project approval."""

from celery import Celery

from app.config import get_settings
from app.telemetry import install_worker_signals

settings = get_settings()
install_worker_signals()
celery_app = Celery("video", broker=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=3,
    worker_prefetch_multiplier=1,
    worker_concurrency=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_soft_time_limit=150,
    task_time_limit=180,
    broker_transport_options={
        "visibility_timeout": 7200,
        "socket_timeout": 3,
        "socket_connect_timeout": 3,
        "retry_on_timeout": False,
    },
    task_routes={
        "video.prepare_render": {"queue": "narration"},
        "video.render_video": {"queue": "rendering"},
        "video.*": {"queue": "generation"},
    },
    worker_hijack_root_logger=False,
    beat_schedule={
        "recover-durable-jobs": {
            "task": "video.reconcile_pending",
            "schedule": 30.0,
            "options": {"queue": "generation", "expires": 25},
        },
        "recover-director-jobs": {
            "task": "video.reconcile_directors",
            "schedule": 30.0,
            "options": {"queue": "generation", "expires": 25},
        },
        "recover-production-jobs": {
            "task": "video.reconcile_production",
            "schedule": 30.0,
            "options": {"queue": "generation", "expires": 25},
        },
    },
)


@celery_app.task(name="video.health", autoretry_for=(), max_retries=0)
def health():
    return {"status": "ok", "phase": 5}


@celery_app.task(name="video.submit_generation", autoretry_for=(), max_retries=0)
def submit_generation(job_id):
    from app.tasks import submit_generation as run

    return run(job_id)


@celery_app.task(name="video.poll_generation", autoretry_for=(), max_retries=0)
def poll_generation(job_id):
    from app.tasks import poll_generation as run

    return run(job_id)


@celery_app.task(name="video.cancel_generation", autoretry_for=(), max_retries=0)
def cancel_generation(job_id):
    from app.tasks import cancel_generation as run

    return run(job_id)


@celery_app.task(name="video.reconcile_pending", autoretry_for=(), max_retries=0)
def reconcile_pending():
    from app.tasks import reconcile_pending as run

    return run()


@celery_app.task(name="video.plan_storyboard", autoretry_for=(), max_retries=0)
def plan_storyboard(job_id):
    from app.director_tasks import plan_storyboard as run

    return run(job_id)


@celery_app.task(name="video.reconcile_directors", autoretry_for=(), max_retries=0)
def reconcile_directors():
    from app.director_tasks import reconcile_directors as run

    return run()


@celery_app.task(
    name="video.prepare_render",
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=1200,
    time_limit=1260,
)
def prepare_render(job_id):
    from app.production_tasks import prepare_render as run

    return run(job_id)


@celery_app.task(
    name="video.render_video",
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=3660,
    time_limit=3720,
)
def render_video(job_id):
    from app.production_tasks import render_video as run

    return run(job_id)


@celery_app.task(name="video.reconcile_production", autoretry_for=(), max_retries=0)
def reconcile_production():
    from app.production_tasks import reconcile_production as run

    return run()
