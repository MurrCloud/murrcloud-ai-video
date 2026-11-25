import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from app.limits import LimiterUnavailable, RateLimitExceeded, RedisLimiter
from redis.exceptions import ConnectionError
from sqlalchemy.engine import make_url


def test_redis_outage_is_not_an_implicit_rate_limit_bypass(monkeypatch):
    limiter = RedisLimiter("redis://localhost:6379/1")
    try:

        def unavailable(*_args, **_kwargs):
            raise ConnectionError("test outage")

        monkeypatch.setattr(limiter.redis, "eval", unavailable)
        with pytest.raises(LimiterUnavailable):
            limiter.check("login-email:private@example.com", 5)
    finally:
        limiter.close()


def test_rate_limit_keys_do_not_contain_email_or_ip(monkeypatch):
    limiter = RedisLimiter("redis://localhost:6379/1")
    captured = []
    try:

        def capture(*args):
            captured.append(args)
            return 1

        monkeypatch.setattr(limiter.redis, "eval", capture)
        limiter.check("login-email:private@example.com", 5, seconds=60)
        key = captured[0][2]
        assert key.startswith("video:rate:")
        assert "private" not in key and "example.com" not in key
        assert captured[0][3] == 60
    finally:
        limiter.close()


@pytest.mark.integration
def test_live_redis_lua_is_atomic_and_sets_expiry():
    url = os.environ.get("VIDEO_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set VIDEO_TEST_REDIS_URL to a dedicated Redis test database /1")
    parsed = make_url(url)
    if parsed.drivername not in {"redis", "rediss"} or parsed.database != "1":
        pytest.fail("Redis integration tests require a dedicated test database /1")
    limiter = RedisLimiter(url)
    raw_key = "integration:" + uuid4().hex
    storage_key = "video:rate:" + hashlib.sha256(raw_key.encode()).hexdigest()
    try:
        assert limiter.ping()

        def attempt(_index):
            try:
                limiter.check(raw_key, 5, seconds=10)
                return True
            except RateLimitExceeded:
                return False

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(attempt, range(32)))
        assert results.count(True) == 5
        assert results.count(False) == 27
        assert limiter.redis.get(storage_key) == "32"
        assert 0 < limiter.redis.ttl(storage_key) <= 10
    finally:
        # Never FLUSHDB: remove only the randomized key created by this test.
        limiter.redis.delete(storage_key)
        limiter.close()
