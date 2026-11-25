import pytest
from app.config import Settings
from cryptography.fernet import Fernet
from pydantic import ValidationError

SECRET = "offline-unit-session-" * 3


def configure(**overrides):
    values = dict(
        _env_file=None,
        environment="test",
        database_url="sqlite://",
        redis_url="redis://localhost:6379/0",
        session_secret=SECRET,
        allowed_origins=["http://localhost:3000"],
        cookie_secure=False,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_secret": "short"},
        {"session_secret": "change-me-" * 8},
        {"environment": "development", "database_url": "sqlite:///local.db"},
        {"database_url": "postgresql+psycopg://user:pass@host/unrelated_app"},
        {"database_url": "postgresql://user:pass@host/video_studio"},
        {"redis_url": "https://redis.invalid"},
        {"allowed_origins": []},
        {"allowed_origins": ["*"]},
        {"allowed_origins": ["http://localhost:3000/path"]},
        {"allowed_origins": ["http://user:password@localhost:3000"]},
        {"allowed_origins": ["javascript:alert(1)"]},
        {"session_hours": 0},
        {"session_hours": 25},
        {"api_requests_per_minute": 0},
        {"login_attempts_per_minute": 0},
        {"paid_generation_enabled": True},
        {"render_timeout_seconds": 3601},
    ],
)
def test_unsafe_configuration_is_rejected(overrides):
    with pytest.raises(ValidationError):
        configure(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"cookie_secure": False},
        {"allowed_origins": ["http://video.example.test"]},
        {"redis_url": "redis://redis:6379/0"},
        {"database_url": "postgresql+psycopg://video@postgres/video_studio"},
    ],
)
def test_production_requires_https_and_separate_credentials(overrides):
    values = dict(
        environment="production",
        cookie_secure=True,
        database_url="postgresql+psycopg://video:unique-db-pass@postgres/video_studio",
        redis_url="redis://:unique-broker-pass@redis:6379/0",
        allowed_origins=["https://video.example.test"],
    )
    values.update(overrides)
    with pytest.raises(ValidationError):
        configure(**values)


def test_valid_production_configuration_keeps_secret_masked():
    settings = configure(
        environment="production",
        cookie_secure=True,
        database_url="postgresql+psycopg://video:unique-db-pass@postgres/video_studio",
        redis_url="redis://:unique-broker-pass@redis:6379/0",
        allowed_origins=["https://video.example.test"],
    )
    assert SECRET not in repr(settings.session_secret)
    assert settings.paid_generation_enabled is False
    assert settings.render_enabled is False


def test_provider_storage_master_is_optional_masked_and_never_enables_generation():
    assert configure().provider_key_encryption_key.get_secret_value() == ""
    key = Fernet.generate_key().decode()
    settings = configure(provider_key_encryption_key=key)
    assert key not in repr(settings)
    assert not settings.paid_generation_enabled and not settings.director_enabled


@pytest.mark.parametrize(
    "key", ["short", "!" * 43 + "=", "a" * 44, "a" * 43 + "=", "a" * 42 + "=="]
)
def test_provider_storage_rejects_invalid_or_noncanonical_master_without_echo(key):
    with pytest.raises(ValidationError) as failure:
        configure(provider_key_encryption_key=key)
    assert key not in str(failure.value)


@pytest.mark.parametrize(
    "overrides,sentinel",
    [
        ({"session_secret": "secret-sentinel-too-short"}, "secret-sentinel-too-short"),
        (
            {"database_url": "postgresql+psycopg://name:secret-db-sentinel@host/unrelated_app"},
            "secret-db-sentinel",
        ),
    ],
)
def test_configuration_validation_does_not_leak_credentials(overrides, sentinel):
    with pytest.raises(ValidationError) as failure:
        configure(**overrides)
    assert sentinel not in str(failure.value)
    assert SECRET not in str(failure.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"director_enabled": True},
        {
            "director_enabled": True,
            "openai_key": "offline-key",
            "director_model": "offline-fixture",
        },
        {"director_model": "invalid model"},
        {"director_timeout_seconds": 91},
        {"director_max_output_tokens": 16001},
        {"director_timeout_seconds": 90, "director_stale_seconds": 100},
        {"storyboard_duration_tolerance_seconds": 11},
        {"storyboard_max_scenes": 121},
    ],
)
def test_director_execution_is_bounded_and_requires_operator_gate(overrides):
    with pytest.raises(ValidationError):
        configure(**overrides)


def test_openai_key_is_private_and_does_not_enable_director(monkeypatch):
    monkeypatch.setenv("VIDEO_OPENAI_API_KEY", "offline-key-sentinel")
    settings = configure(director_model="offline-fixture")
    assert settings.openai_key.get_secret_value() == "offline-key-sentinel"
    assert "offline-key-sentinel" not in repr(settings)
    assert settings.director_enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"tts_timeout_seconds": 61},
        {"render_lease_seconds": 10},
        {"render_scratch_max_bytes": 129 * 1024**2},
        {"render_max_attempts": 6},
        {"s3_enabled": True},
    ],
)
def test_production_processing_limits_and_archive_credentials_are_explicit(overrides):
    with pytest.raises(ValidationError):
        configure(**overrides)


def test_local_render_enable_does_not_enable_remote_calls():
    settings = configure(render_enabled=True)
    assert settings.paid_generation_enabled is False
    assert settings.director_enabled is False
    assert settings.narration_enabled is False
    assert settings.s3_enabled is False
