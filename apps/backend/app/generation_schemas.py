"""Owner-scoped generation inputs and credential-free API responses."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.providers.base import VideoRequest


class GenerationEstimateIn(VideoRequest):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    scene_id: UUID | None = None
    # Reference URLs remain internal until owned reference assets are supported.
    reference_image_url: None = None
    retry_allowance_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100, decimal_places=2)


class GenerationApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    estimate_id: UUID
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    approved_cap_usd: Decimal = Field(
        gt=0,
        le=Decimal("99999999"),
        max_digits=14,
        decimal_places=6,
        description="Total project approval cap, bounded by the project budget; not a per-job cap.",
    )
    approve_unverified_price: bool = False


class GenerationEstimateOut(BaseModel):
    id: UUID
    base_usd: Decimal | None
    total_usd: Decimal | None
    pricing_verified: bool
    source: str
    expires_at: datetime
    request: VideoRequest
    scene_id: UUID | None
    retry_allowance_percent: Decimal


class GenerationJobOut(BaseModel):
    id: UUID
    project_id: UUID
    scene_id: UUID
    scene_version_id: UUID
    version_number: int
    model_id: str
    status: str
    request: VideoRequest
    estimated_cost_usd: Decimal | None
    reserved_cost_usd: Decimal
    actual_cost_usd: Decimal | None
    actual_cost_status: Literal["unavailable", "confirmed"]
    pricing_verified: bool
    no_longer_wanted: bool
    last_error_code: str | None
    dispatch_state: str
    dispatch_error_code: str | None
    next_poll_at: datetime | None
    output_asset_id: UUID | None
    download_url: str | None
    created_at: datetime
    updated_at: datetime
