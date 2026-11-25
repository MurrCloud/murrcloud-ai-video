import hashlib

from redis import Redis


class RateLimitExceeded(Exception):
    pass


class LimiterUnavailable(Exception):
    pass


class RedisLimiter:
    _SCRIPT = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    return count
    """

    def __init__(self, redis_url: str):
        self.redis = Redis.from_url(
            redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
        )

    def check(self, key: str, limit: int, seconds: int = 60) -> None:
        # Email/IP never become plaintext Redis key names.
        key = "video:rate:" + hashlib.sha256(key.encode()).hexdigest()
        try:
            count = self.redis.eval(self._SCRIPT, 1, key, seconds)
        except Exception as exc:
            raise LimiterUnavailable from exc
        if count > limit:
            raise RateLimitExceeded

    def ping(self) -> bool:
        return bool(self.redis.ping())

    def close(self) -> None:
        self.redis.close()
