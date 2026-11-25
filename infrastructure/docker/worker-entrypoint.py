"""Start isolated workers after host admission; tracked gates remain disabled."""

import os
import sys

from admission import evaluate


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"generation", "narration", "rendering"}:
        raise SystemExit("A supported Video Studio queue is required.")
    if os.environ.get("VIDEO_RENDER_ENABLED") not in {"false", "true"}:
        raise SystemExit("An explicit rendering gate is required.")
    if os.environ.get("VIDEO_PAID_GENERATION_ENABLED") not in {"false", "true"}:
        raise SystemExit("An explicit generation gate is required.")
    allowed, reason = evaluate(
        os.environ.get("VIDEO_HOST_SIGNAL_PATH", "/run/video-host/host.json")
    )
    if not allowed:
        raise SystemExit(f"Host admission refused: {reason}")
    queue = sys.argv[1]
    command = [
        "celery",
        "-A",
        "app.worker:celery_app",
        "worker",
        f"--hostname=video-{queue}@%h",
        f"--queues={queue}",
        "--concurrency=1",
        "--pool=prefork",
        "--prefetch-multiplier=1",
        "--without-gossip",
        "--without-mingle",
        "--loglevel=WARNING",
    ]
    if queue == "generation":
        command += ["--beat", "--schedule=/tmp/video-celerybeat", "--pidfile="]
    os.execvp(
        "celery",
        command,
    )


if __name__ == "__main__":
    main()
