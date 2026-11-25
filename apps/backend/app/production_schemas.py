"""Composition is an explicit, bounded snapshot independent of paid generation."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    narration_enabled: bool = True
    language: Literal["en", "zh-CN"] = "en"
    voice: str = Field(default="en-US-AriaNeural", max_length=100)
    speaking_rate: Decimal = Field(
        default=Decimal("1"), ge=Decimal("0.5"), le=Decimal("2"), decimal_places=2
    )
    burn_subtitles: bool = True
    opening_title: str = Field(default="", max_length=200)
    closing_title: str = Field(default="", max_length=200)
    title_duration_seconds: int = Field(default=2, ge=1, le=5)
    music_asset_id: UUID | None = None
    watermark_asset_id: UUID | None = None
    music_volume: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)
    source_audio_volume: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    allow_upscale: bool = False

    @model_validator(mode="after")
    def valid_plan(self):
        from app.tts import TTSError, validate_voice

        try:
            validate_voice(self.language, self.voice)
        except TTSError:
            raise ValueError("Voice must match a supported language") from None
        if self.burn_subtitles and not self.narration_enabled:
            raise ValueError("Subtitles require aligned narration")
        if any(ord(c) < 32 and c not in "\n\t" for c in self.opening_title + self.closing_title):
            raise ValueError("Title control characters are not allowed")
        return self


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=0)
    settings: ProductionSettings


class RenderIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    scene_versions: dict[UUID, UUID] = Field(default_factory=dict, max_length=120)
