"""Strict editor contracts; incomplete drafts remain saveable and are validated separately."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SceneDraft(StrictInput):
    id: UUID | None = None
    scene_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    order: int = Field(strict=True, ge=1, le=120)
    duration_seconds: int = Field(strict=True, ge=1, le=600)
    generation_mode: Literal["text_to_video", "image_to_video"] = "text_to_video"
    video_model_id: str = Field(min_length=1, max_length=160)
    resolution: str = Field(min_length=1, max_length=20)
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4"]
    audio_enabled: bool = Field(strict=True)
    visual_prompt: str = Field(default="", max_length=5000)
    negative_prompt: str = Field(default="", max_length=2500)
    narration: str = Field(default="", max_length=5000)
    reference_asset_ids: list[UUID] = Field(default_factory=list, max_length=10)
    transition: Literal["cut", "fade", "dissolve"] = "fade"
    status: Literal["draft"] = "draft"


class StoryboardPut(StrictInput):
    expected_revision: int = Field(strict=True, ge=0)
    title: str = Field(default="", max_length=200)
    target_duration_seconds: int = Field(strict=True, ge=5, le=600)
    language: Literal["en", "zh-CN"]
    visual_style: str = Field(default="cinematic", max_length=200)
    target_audience: str = Field(default="", max_length=500)
    default_model_id: str | None = Field(default=None, max_length=160)
    scenes: list[SceneDraft] = Field(default_factory=list, max_length=120)


class RevisionIn(StrictInput):
    expected_revision: int = Field(strict=True, ge=0)


class BoardEstimateIn(RevisionIn):
    retry_allowance_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100, decimal_places=2)


class BoardApprovalIn(RevisionIn):
    estimate_id: UUID
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    approved_cap_usd: Decimal = Field(gt=0, le=Decimal("99999999"), max_digits=14, decimal_places=6)
    approve_unverified_price: bool = False


class InputIn(StrictInput):
    kind: Literal["prompt", "script"]
    text: str = Field(min_length=1, max_length=100000)
    source_name: str | None = Field(default=None, max_length=255)


class DirectorApprovalIn(RevisionIn):
    input_id: UUID
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    max_cost_usd: Decimal | None = Field(
        default=None, gt=0, le=Decimal("99999999"), max_digits=14, decimal_places=6
    )
    approved_cap_usd: Decimal | None = Field(
        default=None, gt=0, le=Decimal("99999999"), max_digits=14, decimal_places=6
    )
    approve_unknown_price: bool = False

    @model_validator(mode="after")
    def explicit_cap(self):
        if self.max_cost_usd is None and self.approved_cap_usd is None:
            raise ValueError("An explicit director spending cap is required")
        return self
