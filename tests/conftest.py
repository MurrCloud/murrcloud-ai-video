"""Deterministic unit-test services; no network or production credentials are used."""

import os
from collections import defaultdict
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

# app.main exports an ASGI app. Its import must use an isolated unit-test config.
os.environ["VIDEO_ENVIRONMENT"] = "test"
os.environ["VIDEO_DATABASE_URL"] = "sqlite://"
os.environ["VIDEO_REDIS_URL"] = "redis://localhost:6379/0"
os.environ["VIDEO_SESSION_SECRET"] = "offline-unit-session-" * 3

from app.config import Settings  # noqa: E402
from app.db import make_engine, make_session_factory  # noqa: E402
from app.limits import LimiterUnavailable, RateLimitExceeded  # noqa: E402
from app.main import create_app  # noqa: E402
from app.migration_head import SCHEMA_HEAD  # noqa: E402
from app.models import Base, User  # noqa: E402
from app.security import hash_password  # noqa: E402

PASSWORD = "local-test-password-123!"
PASSWORD_HASH = hash_password(PASSWORD)
ORIGIN = "http://localhost:3000"


@pytest.fixture(autouse=True)
def prevent_unmocked_async_provider_requests(monkeypatch):
    """Every provider test must use MockTransport, even with a test approval flag."""

    async def blocked(_transport, _request):
        raise AssertionError("Unmocked provider HTTP is disabled in automated tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)


class FakeLimiter:
    """Explicit test double, never an implicit production fallback."""

    def __init__(self):
        self.counts = defaultdict(int)
        self.unavailable = False
        self.ping_ok = True
        self.closed = False

    def check(self, key, limit, seconds=60):
        if self.unavailable:
            raise LimiterUnavailable
        self.counts[key] += 1
        if self.counts[key] > limit:
            raise RateLimitExceeded

    def ping(self):
        if self.unavailable:
            raise LimiterUnavailable
        return self.ping_ok

    def close(self):
        self.closed = True


@pytest.fixture
def api():
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="sqlite://",
        redis_url="redis://localhost:6379/0",
        session_secret=os.environ["VIDEO_SESSION_SECRET"],
        allowed_origins=[ORIGIN],
        cookie_secure=False,
        api_requests_per_minute=1000,
        login_attempts_per_minute=20,
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:head)"), {"head": SCHEMA_HEAD}
        )

    def create_user(email, role="editor", active=True):
        with sessions() as db:
            user = User(email=email, role=role, active=active, password_hash=PASSWORD_HASH)
            db.add(user)
            db.commit()
            return user.id

    user_ids = {
        "editor": create_user("editor@example.com"),
        "other": create_user("other@example.com"),
        "viewer": create_user("viewer@example.com", "viewer"),
        "admin": create_user("admin@example.com", "admin"),
        "disabled": create_user("disabled@example.com", active=False),
    }
    limiter = FakeLimiter()
    application = create_app(settings, engine=engine, limiter=limiter)
    with TestClient(application, base_url="http://testserver") as client:
        yield SimpleNamespace(
            client=client,
            app=application,
            sessions=sessions,
            limiter=limiter,
            user_ids=user_ids,
            create_user=create_user,
            settings=settings,
        )


@pytest.fixture
def login(api):
    def authenticate(role="editor", **kwargs):
        response = api.client.post(
            "/api/v1/auth/login",
            json={"email": f"{role}@example.com", "password": PASSWORD},
            headers={"Origin": ORIGIN},
            **kwargs,
        )
        assert response.status_code == 200, response.text
        return response

    return authenticate


@pytest.fixture(autouse=True)
def prevent_unmocked_speech_and_object_storage(monkeypatch):
    """Network speech/S3 tests must inject transports; real FFmpeg stays local."""
    import asyncio
    import importlib.util

    async def blocked_child(*_args, **_kwargs):
        raise AssertionError("Unmocked speech subprocess disabled in automated tests")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked_child)
    if importlib.util.find_spec("botocore") is not None:
        import botocore.endpoint

        def blocked_request(*_args, **_kwargs):
            raise AssertionError("Unmocked S3 requests disabled in automated tests")

        monkeypatch.setattr(botocore.endpoint.Endpoint, "make_request", blocked_request)
