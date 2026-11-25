"""Factories keep web/worker configuration separate from provider transport."""


def make_provider(settings):
    from app.providers.fal import FalProvider

    return FalProvider(api_key=settings.fal_key)


def make_director(settings):
    from app.director import OpenAIDirector

    return OpenAIDirector(
        api_key=settings.openai_key,
        model=settings.director_model,
        max_output_tokens=settings.director_max_output_tokens,
        timeout_seconds=settings.director_timeout_seconds,
    )


def make_tts(settings):
    from app.tts import EdgeTTS

    return EdgeTTS(timeout_seconds=settings.tts_timeout_seconds)


def dispatch_production(task_name: str, job_id: str, countdown: int = 0):
    queues = {"video.prepare_render": "narration", "video.render_video": "rendering"}
    if task_name not in queues:
        raise ValueError("Unsupported production operation")
    from app.worker import celery_app

    return celery_app.send_task(
        task_name,
        args=[str(job_id)],
        queue=queues[task_name],
        countdown=countdown,
        retry=False,
        expires=86400,
    )


def dispatch_director(task_name: str, job_id: str, countdown: int = 0):
    if task_name != "video.plan_storyboard":
        raise ValueError("Unsupported director operation")
    from app.worker import celery_app

    return celery_app.send_task(
        task_name,
        args=[str(job_id)],
        queue="generation",
        countdown=countdown,
        retry=False,
        expires=86400,
    )


def dispatch_generation(task_name: str, job_id: str, countdown: int = 0):
    if task_name not in {
        "video.submit_generation",
        "video.poll_generation",
        "video.cancel_generation",
    }:
        raise ValueError("Unsupported generation operation")
    from app.worker import celery_app

    return celery_app.send_task(
        task_name,
        args=[str(job_id)],
        queue="generation",
        countdown=countdown,
        retry=False,
        expires=86400,
    )
