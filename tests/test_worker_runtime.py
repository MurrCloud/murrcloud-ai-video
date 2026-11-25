"""Only a free health task is sent through a real disposable Celery broker."""

import os
import time
from urllib.parse import urlsplit

import pytest
from celery import Celery


@pytest.mark.integration
def test_real_worker_consumes_only_a_health_task():
    if os.environ.get("VIDEO_TEST_WORKER_ENABLED") != "true":
        pytest.skip("Disposable Celery worker not enabled")
    url = os.environ["VIDEO_LIVE_REDIS_URL"]
    assert urlsplit(url).hostname == "redis" and urlsplit(url).path == "/0"
    client = Celery("video-ci-health", broker=url)
    client.conf.update(
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        broker_connection_timeout=3,
        broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    )
    try:
        stats = client.control.inspect(timeout=3).stats()
        assert stats, "Generation worker must answer inspection"
        before = sum(node.get("total", {}).get("video.health", 0) for node in stats.values())
        # This fixed task is the only message published by this integration test.
        client.send_task("video.health", queue="generation", retry=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            stats = client.control.inspect(timeout=3).stats() or {}
            count = sum(node.get("total", {}).get("video.health", 0) for node in stats.values())
            if count > before:
                break
            time.sleep(0.2)
        else:
            pytest.fail("Health task was not consumed before the deadline")
        active = client.control.inspect(timeout=3).active() or {}
        assert not any(
            task.get("name") == "video.health" for tasks in active.values() for task in tasks
        )
    finally:
        client.close()
