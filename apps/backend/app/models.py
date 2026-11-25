"""Relational foundation. Provider secrets are stored only as staged ciphertext.

Scene versions are append-only snapshots. A restoration creates a new version;
it does not replace the previous snapshot or its generation/cost history.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
MONEY = Numeric(14, 6)


def utcnow() -> datetime:
    return datetime.now(UTC)


def state_check(column: str, states: str, name: str | None = None) -> CheckConstraint:
    allowed = ", ".join(repr(value) for value in states.split())
    return CheckConstraint(f"{column} IN ({allowed})", name=name or f"{column}_values")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class Identity:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )


class Mutable:
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class User(Identity, Mutable, Base):
    __tablename__ = "users"
    __table_args__ = (state_check("role", "admin editor viewer"),)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="editor", server_default="editor")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class AuthSession(Identity, Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_expiry", "user_id", "expires_at"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderConfiguration(Identity, Mutable, Base):
    __tablename__ = "provider_configurations"
    provider: Mapped[str] = mapped_column(String(64), unique=True)
    credential_env_var: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # Safe operational settings only; API keys/tokens are forbidden here.
    settings: Mapped[dict] = mapped_column(DOCUMENT, default=dict)


class ProviderSecret(Base):
    """A staged, authenticated ciphertext; never an automatically active credential."""

    __tablename__ = "provider_secrets"
    __table_args__ = (
        state_check("provider", "openai fal"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        CheckConstraint(
            "encrypted_value IS NULL OR length(encrypted_value) <= 2048",
            name="ciphertext_bounded",
        ),
    )
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    encrypted_value: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class VideoModel(Identity, Mutable, Base):
    __tablename__ = "video_models"
    __table_args__ = (
        CheckConstraint(
            "min_duration_seconds > 0 AND max_duration_seconds >= min_duration_seconds",
            name="duration_range",
        ),
        CheckConstraint("price_usd IS NULL OR price_usd >= 0", name="price_nonnegative"),
        Index(
            "uq_video_models_one_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default = true"),
            sqlite_where=text("is_default = 1"),
        ),
    )
    model_id: Mapped[str] = mapped_column(String(160), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(
        ForeignKey("provider_configurations.provider", ondelete="RESTRICT"), index=True
    )
    endpoint_id: Mapped[str] = mapped_column(String(255))
    supported_generation_modes: Mapped[list] = mapped_column(DOCUMENT, default=list)
    supported_resolutions: Mapped[list] = mapped_column(DOCUMENT, default=list)
    supported_aspect_ratios: Mapped[list] = mapped_column(DOCUMENT, default=list)
    min_duration_seconds: Mapped[int] = mapped_column(Integer)
    max_duration_seconds: Mapped[int] = mapped_column(Integer)
    supports_reference_images: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    supports_audio: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    price_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    pricing_unit: Mapped[str | None] = mapped_column(String(64))
    pricing_details: Mapped[dict] = mapped_column(DOCUMENT, default=dict)
    documentation_url: Mapped[str | None] = mapped_column(String(1024))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Project(Identity, Mutable, Base):
    __tablename__ = "projects"
    __table_args__ = (
        state_check(
            "status",
            "draft planning awaiting_approval generating clips_ready narrating rendering completed failed cancelled",
        ),
        CheckConstraint("target_duration_seconds > 0", name="duration_positive"),
        CheckConstraint("budget_usd >= 0 AND committed_cost_usd >= 0", name="budget_nonnegative"),
        CheckConstraint(
            "approved_cap_usd IS NULL OR (approved_cap_usd >= 0 AND approved_cap_usd <= budget_usd)",
            name="approved_cap_within_budget",
        ),
        CheckConstraint(
            "committed_cost_usd = 0 OR (approved_cap_usd IS NOT NULL AND committed_cost_usd <= approved_cap_usd)",
            name="committed_within_approval",
        ),
        CheckConstraint("currency = 'USD'", name="currency_usd"),
        Index("ix_projects_owner_updated", "owner_id", "updated_at"),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(32), default="draft", server_default="draft")
    target_duration_seconds: Mapped[int] = mapped_column(Integer, default=15, server_default="15")
    budget_usd: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), server_default="0")
    approved_cap_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    committed_cost_usd: Mapped[Decimal] = mapped_column(
        MONEY, default=Decimal("0"), server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD", server_default="USD")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class ProjectSettings(Identity, Mutable, Base):
    __tablename__ = "project_settings"
    __table_args__ = (
        state_check("aspect_ratio", "16:9 9:16 1:1"),
        state_check("language", "en zh-CN"),
        state_check("generation_mode", "text_to_video image_to_video"),
        CheckConstraint(
            "speaking_rate >= 0.5 AND speaking_rate <= 2.0", name="speaking_rate_range"
        ),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True
    )
    aspect_ratio: Mapped[str] = mapped_column(String(8), default="16:9", server_default="16:9")
    resolution: Mapped[str] = mapped_column(String(20), default="1080p", server_default="1080p")
    language: Mapped[str] = mapped_column(String(20), default="en", server_default="en")
    visual_style: Mapped[str] = mapped_column(
        String(200), default="cinematic", server_default="cinematic"
    )
    generation_mode: Mapped[str] = mapped_column(
        String(32), default="text_to_video", server_default="text_to_video"
    )
    default_model_id: Mapped[str | None] = mapped_column(
        ForeignKey("video_models.model_id", ondelete="RESTRICT")
    )
    narration_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    voice: Mapped[str | None] = mapped_column(String(100))
    speaking_rate: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), default=Decimal("1"), server_default="1"
    )
    composition: Mapped[dict] = mapped_column(DOCUMENT, default=dict)


class Scene(Identity, Mutable, Base):
    __tablename__ = "scenes"
    __table_args__ = (
        UniqueConstraint("project_id", "scene_key"),
        UniqueConstraint("project_id", "order_index"),
        CheckConstraint("order_index > 0 AND duration_seconds > 0", name="positive_order_duration"),
        state_check("generation_mode", "text_to_video image_to_video"),
        state_check("status", "draft approved queued generating completed failed cancelled"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    scene_key: Mapped[str] = mapped_column(String(64))
    order_index: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[int] = mapped_column(Integer)
    generation_mode: Mapped[str] = mapped_column(
        String(32), default="text_to_video", server_default="text_to_video"
    )
    video_model_id: Mapped[str] = mapped_column(
        ForeignKey("video_models.model_id", ondelete="RESTRICT")
    )
    visual_prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    narration: Mapped[str] = mapped_column(Text, default="", server_default="")
    transition: Mapped[str] = mapped_column(String(32), default="fade", server_default="fade")
    status: Mapped[str] = mapped_column(String(24), default="draft", server_default="draft")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    generation_settings: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")
    reference_asset_ids: Mapped[list] = mapped_column(DOCUMENT, default=list, server_default="[]")


class SceneVersion(Identity, Base):
    __tablename__ = "scene_versions"
    __table_args__ = (
        UniqueConstraint("scene_id", "version_number"),
        CheckConstraint(
            "version_number > 0 AND duration_seconds > 0", name="positive_version_duration"
        ),
        state_check(
            "status", "draft approved queued generating completed failed cancelled restored"
        ),
    )
    scene_id: Mapped[UUID] = mapped_column(ForeignKey("scenes.id", ondelete="RESTRICT"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    created_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    video_model_id: Mapped[str] = mapped_column(
        ForeignKey("video_models.model_id", ondelete="RESTRICT")
    )
    generation_mode: Mapped[str] = mapped_column(String(32))
    duration_seconds: Mapped[int] = mapped_column(Integer)
    visual_prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    narration: Mapped[str] = mapped_column(Text, default="", server_default="")
    settings_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="draft", server_default="draft")
    restored_from_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scene_versions.id", ondelete="RESTRICT")
    )


class GenerationJob(Identity, Mutable, Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        UniqueConstraint("provider", "provider_job_id"),
        state_check(
            "status",
            "pending approved submitting submitted queued running succeeded failed cancellation_requested cancelled reconciliation_required abandoned",
        ),
        CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="estimated_cost_nonnegative",
        ),
        CheckConstraint("reserved_cost_usd >= 0", name="reservation_nonnegative"),
        CheckConstraint("poll_count >= 0", name="poll_count_nonnegative"),
        state_check("dispatch_state", "pending queued error not_needed"),
        Index("ix_generation_jobs_status_updated", "status", "updated_at"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    scene_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scene_versions.id", ondelete="RESTRICT"), index=True
    )
    model_id: Mapped[str] = mapped_column(ForeignKey("video_models.model_id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(
        ForeignKey("provider_configurations.provider", ondelete="RESTRICT")
    )
    endpoint_id: Mapped[str] = mapped_column(String(255))
    provider_job_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    request_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    no_longer_wanted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    estimate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cost_estimates.id", ondelete="RESTRICT"), unique=True
    )
    model_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")
    submission_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")
    reserved_cost_usd: Mapped[Decimal] = mapped_column(
        MONEY, default=Decimal("0"), server_default="0"
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_fingerprint: Mapped[str | None] = mapped_column(String(64))
    output_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT")
    )
    poll_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    dispatch_state: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    dispatch_error_code: Mapped[str | None] = mapped_column(String(100))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_token: Mapped[UUID | None] = mapped_column(Uuid)
    operation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationAttempt(Identity, Base):
    __tablename__ = "generation_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number"),
        CheckConstraint("attempt_number > 0", name="attempt_positive"),
        state_check("status", "started succeeded failed ambiguous reconciled"),
    )
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="RESTRICT"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="started", server_default="started")
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(100))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaAsset(Identity, Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
        state_check(
            "kind",
            "reference_image script clip narration subtitles music logo final_video thumbnail",
        ),
        state_check("status", "pending ready rejected deleted"),
        state_check("storage_backend", "local s3"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    uploaded_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    scene_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scene_versions.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_backend: Mapped[str] = mapped_column(
        String(16), default="local", server_default="local"
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    metadata_json: Mapped[dict] = mapped_column(DOCUMENT, default=dict)


class SceneReferenceAsset(Identity, Base):
    __tablename__ = "scene_reference_assets"
    __table_args__ = (UniqueConstraint("scene_version_id", "asset_id"),)
    scene_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scene_versions.id", ondelete="RESTRICT"), index=True
    )
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("media_assets.id", ondelete="RESTRICT"))


class NarrationAsset(Identity, Base):
    __tablename__ = "narration_assets"
    scene_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scene_versions.id", ondelete="RESTRICT"), index=True
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    voice: Mapped[str] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(20))
    duration_seconds: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    alignment_data: Mapped[list] = mapped_column(DOCUMENT, default=list)
    fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True)
    speaking_rate: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), default=Decimal("1"), server_default="1"
    )


class SubtitleAsset(Identity, Base):
    __tablename__ = "subtitle_assets"
    __table_args__ = (state_check("alignment_source", "speech_boundaries forced_alignment manual"),)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    scene_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scene_versions.id", ondelete="RESTRICT")
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), unique=True
    )
    language: Mapped[str] = mapped_column(String(20))
    format: Mapped[str] = mapped_column(String(16), default="srt", server_default="srt")
    alignment_source: Mapped[str] = mapped_column(String(32))


class RenderingJob(Identity, Mutable, Base):
    __tablename__ = "rendering_jobs"
    __table_args__ = (
        state_check("status", "pending admitted running succeeded failed cancelled paused"),
        Index("ix_rendering_jobs_status_updated", "status", "updated_at"),
        CheckConstraint(
            "attempt_count >= 0 AND progress_percent >= 0 AND progress_percent <= 100",
            name="render_progress_bounds",
        ),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    composition_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict)
    output_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT")
    )
    worker_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    requested_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(24), default="narration", server_default="narration")
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_state: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    dispatch_error_code: Mapped[str | None] = mapped_column(String(100))
    subtitle_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT")
    )
    warnings: Mapped[list] = mapped_column(DOCUMENT, default=list, server_default="[]")
    validation_report: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")


class CostEstimate(Identity, Base):
    __tablename__ = "cost_estimates"
    __table_args__ = (
        CheckConstraint("total_usd IS NULL OR total_usd >= 0", name="total_nonnegative"),
        CheckConstraint("retry_allowance_percent >= 0", name="allowance_nonnegative"),
        state_check("status", "pending approved rejected expired"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    total_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    pricing_available: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    retry_allowance_percent: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), server_default="0"
    )
    breakdown: Mapped[list] = mapped_column(DOCUMENT, default=list)
    plan_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    approved_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    base_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    pricing_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    pricing_source: Mapped[str] = mapped_column(
        String(128), default="unavailable", server_default="unavailable"
    )
    request_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")
    model_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")
    context_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")
    scene_id: Mapped[UUID | None] = mapped_column(ForeignKey("scenes.id", ondelete="RESTRICT"))
    scene_version_number: Mapped[int | None] = mapped_column(Integer)


class ActualUsageCost(Identity, Mutable, Base):
    __tablename__ = "actual_usage_costs"
    __table_args__ = (
        UniqueConstraint("provider", "provider_charge_id"),
        CheckConstraint("amount_usd IS NULL OR amount_usd >= 0", name="amount_nonnegative"),
        state_check("status", "pending confirmed unavailable"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    scene_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scene_versions.id", ondelete="RESTRICT"), index=True
    )
    generation_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="RESTRICT"), index=True
    )
    rendering_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rendering_jobs.id", ondelete="RESTRICT")
    )
    director_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("director_jobs.id", ondelete="RESTRICT"), index=True
    )
    model_id: Mapped[str | None] = mapped_column(
        ForeignKey("video_models.model_id", ondelete="RESTRICT"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64), index=True)
    provider_charge_id: Mapped[str | None] = mapped_column(String(255))
    amount_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    usage_details: Mapped[dict] = mapped_column(DOCUMENT, default=dict)


class ProjectTemplate(Identity, Mutable, Base):
    __tablename__ = "project_templates"
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    production_plan: Mapped[dict] = mapped_column(DOCUMENT, default=dict)
    shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class WorkspaceMutation(Identity, Base):
    __tablename__ = "workspace_mutations"
    __table_args__ = (UniqueConstraint("actor_user_id", "operation", "idempotency_key"),)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    operation: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    result_project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT")
    )
    result_template_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_templates.id", ondelete="RESTRICT")
    )
    result_usage_cost_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("actual_usage_costs.id", ondelete="RESTRICT")
    )


class WorkerHeartbeat(Identity, Mutable, Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(128), unique=True)
    queue: Mapped[str] = mapped_column(String(24), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditEvent(Identity, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_project_created", "project_id", "created_at"),)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[UUID | None] = mapped_column(Uuid)
    details: Mapped[dict] = mapped_column(DOCUMENT, default=dict)


class ProjectInput(Identity, Base):
    __tablename__ = "project_inputs"
    __table_args__ = (state_check("kind", "prompt script docx"),)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    created_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    warnings: Mapped[list] = mapped_column(DOCUMENT, default=list, server_default="[]")


class Storyboard(Identity, Mutable, Base):
    __tablename__ = "storyboards"
    __table_args__ = (
        state_check("status", "draft validated approved"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), unique=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
    input_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_inputs.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    settings_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")


class StoryboardRevision(Identity, Base):
    __tablename__ = "storyboard_revisions"
    __table_args__ = (
        UniqueConstraint("storyboard_id", "revision"),
        CheckConstraint("revision > 0", name="revision_positive"),
    )
    storyboard_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboards.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    created_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    snapshot: Mapped[dict] = mapped_column(DOCUMENT)


class StoryboardEstimate(Identity, Mutable, Base):
    __tablename__ = "storyboard_estimates"
    __table_args__ = (
        state_check("status", "pending approved expired"),
        CheckConstraint("total_usd IS NULL OR total_usd >= 0", name="total_nonnegative"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    storyboard_id: Mapped[UUID] = mapped_column(ForeignKey("storyboards.id", ondelete="RESTRICT"))
    revision: Mapped[int] = mapped_column(Integer)
    base_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    total_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    pricing_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    retry_allowance_percent: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), server_default="0"
    )
    snapshot: Mapped[dict] = mapped_column(DOCUMENT)
    plan_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    approval_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    approval_fingerprint: Mapped[str | None] = mapped_column(String(64))
    approved_cap_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    approved_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generation_job_ids: Mapped[list] = mapped_column(DOCUMENT, default=list, server_default="[]")


class DirectorJob(Identity, Mutable, Base):
    __tablename__ = "director_jobs"
    __table_args__ = (
        state_check("status", "approved submitting succeeded failed reconciliation_required stale"),
        state_check("dispatch_state", "pending queued error not_needed"),
        CheckConstraint("reserved_cost_usd > 0", name="reservation_positive"),
        Index("ix_director_jobs_status_updated", "status", "updated_at"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    storyboard_id: Mapped[UUID] = mapped_column(ForeignKey("storyboards.id", ondelete="RESTRICT"))
    input_id: Mapped[UUID] = mapped_column(ForeignKey("project_inputs.id", ondelete="RESTRICT"))
    board_revision: Mapped[int] = mapped_column(Integer)
    context_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(160))
    request_snapshot: Mapped[dict] = mapped_column(DOCUMENT)
    result_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(32), default="approved", server_default="approved")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    approval_fingerprint: Mapped[str] = mapped_column(String(64))
    approved_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reserved_cost_usd: Mapped[Decimal] = mapped_column(MONEY)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_id: Mapped[str | None] = mapped_column(String(160), unique=True)
    usage: Mapped[dict] = mapped_column(DOCUMENT, default=dict, server_default="{}")
    result_revision: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    dispatch_state: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    dispatch_error_code: Mapped[str | None] = mapped_column(String(100))


class DirectorAttempt(Identity, Base):
    __tablename__ = "director_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number"),
        CheckConstraint("attempt_number > 0", name="attempt_positive"),
        state_check("status", "started succeeded failed ambiguous stale"),
    )
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("director_jobs.id", ondelete="RESTRICT"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="started", server_default="started")
    error_code: Mapped[str | None] = mapped_column(String(100))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
