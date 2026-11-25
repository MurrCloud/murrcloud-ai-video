"""Stable application contract; endpoint schemas belong only inside adapters."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderError(Exception):
    """A safe code, not a provider response/body/URL containing customer input."""

    def __init__(self, code: str, *, ambiguous: bool = False, retryable: bool = False):
        self.code = code
        self.ambiguous = ambiguous
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    endpoint_id: str
    provider: str
    adapter_profile: str
    modes: tuple[str, ...]
    resolutions: tuple[str, ...]
    aspect_ratios: tuple[str, ...]
    min_duration_seconds: int
    max_duration_seconds: int
    supports_reference_images: bool
    supports_audio: bool
    enabled: bool
    pricing: dict = field(default_factory=dict)
    price_usd: Decimal | None = None
    pricing_unit: str | None = None


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    model_id: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=5000)
    negative_prompt: str = Field(default="", max_length=2500)
    duration_seconds: int = Field(strict=True, ge=1, le=30)
    mode: Literal["text_to_video", "image_to_video"] = "text_to_video"
    resolution: str = Field(min_length=1, max_length=20)
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4"]
    audio_enabled: bool = False
    # Internal adapter input only. Public APIs accept owned asset IDs in Phase 3.
    reference_image_url: str | None = Field(default=None, max_length=2048, repr=False)

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Visual prompt must not be blank")
        return value


@dataclass(frozen=True)
class CostQuote:
    amount_usd: Decimal | None
    source: str
    verified: bool
    basis: dict


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    request_id: str = Field(min_length=1, max_length=128)
    status_url: str = Field(max_length=2048)
    response_url: str = Field(max_length=2048)
    cancel_url: str = Field(max_length=2048)


@dataclass(frozen=True)
class GenerationStatus:
    state: Literal["queued", "running", "completed", "failed", "cancelled"]
    error_code: str | None = None


@dataclass(frozen=True)
class GenerationResult:
    video_url: str
    metadata: dict


@dataclass(frozen=True)
class CancelOutcome:
    state: str
    confirmed_stopped: bool


class VideoProvider(Protocol):
    def reconcile_submission(self, model: ModelSpec, request_id: str) -> Submission: ...
    async def list_models(self) -> list[dict]: ...
    async def validate_request(self, model: ModelSpec, request: VideoRequest) -> dict: ...
    async def estimate_cost(self, model: ModelSpec, request: VideoRequest) -> CostQuote: ...
    async def submit_generation(
        self, model: ModelSpec, request: VideoRequest, *, paid_authorized: bool = False
    ) -> Submission: ...
    async def get_generation_status(
        self, model: ModelSpec, submission: Submission
    ) -> GenerationStatus: ...
    async def retrieve_result(
        self, model: ModelSpec, submission: Submission
    ) -> GenerationResult: ...
    async def cancel_generation(
        self, model: ModelSpec, submission: Submission
    ) -> CancelOutcome: ...
