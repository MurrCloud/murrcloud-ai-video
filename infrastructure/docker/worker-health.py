"""Bounded health check; never logs broker URLs or exception contents."""

import os
import socket
import sys

from app.worker import celery_app


def main() -> int:
    queue = os.environ.get("VIDEO_WORKER_QUEUE", "")
    if queue not in {"generation", "narration", "rendering"}:
        return 1
    try:
        replies = celery_app.control.ping(
            destination=[f"video-{queue}@{socket.gethostname()}"],
            timeout=4,
        )
        return (
            0
            if any(
                "pong" == reply.get(f"video-{queue}@{socket.gethostname()}", {}).get("ok")
                for reply in replies
            )
            else 1
        )
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
