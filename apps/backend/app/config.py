import base64
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VIDEO_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str
    redis_url: str
    session_secret: SecretStr
    allowed_origins: list[str] = [
        "http://localhost:3100",
        "http://127.0.0.1:3100",
        "http://localhost:8100",
        "http://127.0.0.1:8100",
    ]
    cookie_secure: bool = True
    session_hours: int = 8
    api_requests_per_minute: int = 120
    login_attempts_per_minute: int = 5
    temp_dir: Path = Path("/tmp/video-studio")
    storage_dir: Path = Path("/data/video-studio")
    paid_generation_enabled: bool = False
    render_enabled: bool = False
    host_signal_path: Path = Path("/run/video-host/host.json")
    fal_key: SecretStr = SecretStr("")
    # Independent server secret for encrypted, inactive portal credentials.
    provider_key_encryption_key: SecretStr = SecretStr("")
    paid_approval_reference: str = ""
    generation_poll_base_seconds: int = 5
    generation_poll_max_seconds: int = 60
    generation_poll_max_attempts: int = 120
    estimate_ttl_seconds: int = 900
    max_media_bytes: int = 512 * 1024**2
    storage_max_bytes: int = 2 * 1024**3
    storage_min_free_bytes: int = 512 * 1024**2
    download_timeout_seconds: int = 120
    director_enabled: bool = False
    openai_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("VIDEO_OPENAI_API_KEY", "OPENAI_API_KEY", "openai_key"),
    )
    director_model: str = ""
    director_approval_reference: str = ""
    director_max_output_tokens: int = 8000
    director_timeout_seconds: int = 45
    director_stale_seconds: int = 180
    storyboard_duration_tolerance_seconds: int = 1
    storyboard_max_scenes: int = 120
    narration_enabled: bool = False
    tts_timeout_seconds: int = 60
    render_timeout_seconds: int = 3600
    render_lease_seconds: int = 120
    render_max_attempts: int = 3
    render_scratch_max_bytes: int = 128 * 1024**2
    s3_enabled: bool = False
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")

    @field_validator("session_secret")
    @classmethod
    def strong_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret) < 32 or any(
            x in secret.lower() for x in ("change-me", "changeme", "example")
        ):
            raise ValueError("Set a unique random VIDEO_SESSION_SECRET of at least 32 characters")
        return value

    @field_validator("provider_key_encryption_key")
    @classmethod
    def canonical_provider_encryption_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw:
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}=", raw):
                raise ValueError("Use an independent, canonical URL-safe Fernet encryption key")
            decoded = base64.urlsafe_b64decode(raw)
            if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode() != raw:
                raise ValueError("Use an independent, canonical URL-safe Fernet encryption key")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def explicit_origins(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("At least one explicit origin is required")
        for origin in value:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError("Origins must be explicit http(s) origins without paths")
        return value

    @model_validator(mode="after")
    def safety_constraints(self):
        if not 1 <= self.session_hours <= 24:
            raise ValueError("Session lifetime must be 1–24 hours")
        if min(self.api_requests_per_minute, self.login_attempts_per_minute) < 1:
            raise ValueError("Rate limits must be positive")
        if self.database_url.startswith("sqlite"):
            if self.environment != "test":
                raise ValueError("SQLite is allowed only for unit tests")
        else:
            parsed = urlsplit(self.database_url)
            database = parsed.path.lstrip("/")
            if parsed.scheme != "postgresql+psycopg" or not (
                database == "video_studio" or database.startswith("video_studio_")
            ):
                raise ValueError("Use psycopg and a dedicated video_studio database")
        if urlsplit(self.redis_url).scheme not in {"redis", "rediss"}:
            raise ValueError("Invalid Redis URL")
        if self.environment == "production":
            if not self.cookie_secure or any(
                not x.startswith("https://") for x in self.allowed_origins
            ):
                raise ValueError("Production requires HTTPS origins and secure cookies")
            if not urlsplit(self.redis_url).password or not urlsplit(self.database_url).password:
                raise ValueError("Production requires dedicated database and broker passwords")
        if not (
            10 <= self.tts_timeout_seconds <= 60
            and 30 <= self.render_timeout_seconds <= 3600
            and 60 <= self.render_lease_seconds <= 600
            and 1 <= self.render_max_attempts <= 5
            and 16 * 1024**2 <= self.render_scratch_max_bytes <= 128 * 1024**2
        ):
            raise ValueError("Invalid bounded production worker settings")
        if self.s3_enabled:
            endpoint = urlsplit(self.s3_endpoint_url)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.path not in {"", "/"}
                or endpoint.query
                or endpoint.fragment
                or not self.s3_bucket
                or not self.s3_access_key_id.get_secret_value()
                or not self.s3_secret_access_key.get_secret_value()
            ):
                raise ValueError(
                    "Private S3 requires an explicit HTTPS endpoint, bucket and dedicated credentials"
                )
        if self.director_enabled and not (
            self.openai_key.get_secret_value().strip()
            and self.director_model.strip()
            and self.director_approval_reference.strip()
        ):
            raise ValueError(
                "Live director requires an OpenAI key, explicit model and operator approval reference"
            )
        if self.director_model and (
            len(self.director_model) > 128 or any(c.isspace() for c in self.director_model)
        ):
            raise ValueError("Invalid director model identifier")
        if not (
            512 <= self.director_max_output_tokens <= 16000
            and 10 <= self.director_timeout_seconds <= 90
            and self.director_timeout_seconds + 30 <= self.director_stale_seconds <= 600
        ):
            raise ValueError("Invalid bounded director execution settings")
        if not (
            0 <= self.storyboard_duration_tolerance_seconds <= 10
            and 1 <= self.storyboard_max_scenes <= 120
        ):
            raise ValueError("Invalid storyboard validation limits")
        if self.paid_generation_enabled and (
            not self.fal_key.get_secret_value().strip() or not self.paid_approval_reference.strip()
        ):
            raise ValueError(
                "Paid generation requires a provider key and an explicit operator approval reference"
            )
        if not 1 <= self.generation_poll_base_seconds <= self.generation_poll_max_seconds <= 300:
            raise ValueError("Invalid polling backoff")
        if (
            not 1 <= self.generation_poll_max_attempts <= 1000
            or not 60 <= self.estimate_ttl_seconds <= 3600
        ):
            raise ValueError("Invalid polling or estimate lifetime")
        if not (
            1024 <= self.max_media_bytes <= 1024**3
            and self.storage_max_bytes >= self.max_media_bytes
            and self.storage_min_free_bytes >= 1024**2
            and 10 <= self.download_timeout_seconds <= 600
        ):
            raise ValueError("Invalid bounded media storage settings")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
