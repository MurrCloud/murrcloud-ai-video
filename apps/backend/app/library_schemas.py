"""Bounded workspace mutations and portable, media-free production plans."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.production_schemas import ProductionSettings
from app.storyboard_schemas import SceneDraft


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class MutationIn(StrictInput):
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class DuplicateIn(MutationIn):
    name: str = Field(min_length=1, max_length=160)

    @field_validator("name")
    @classmethod
    def visible_name(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("A visible project name without control characters is required")
        return value.strip()


class MetadataIn(StrictInput):
    expected_updated_at: datetime
    name: str = Field(min_length=1, max_length=160)
    budget_usd: Decimal = Field(ge=0, le=10000, decimal_places=4)
    _visible_name = field_validator("name")(DuplicateIn.visible_name.__func__)

    @field_validator("expected_updated_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value.tzinfo is None:
            raise ValueError("An explicit timezone is required")
        return value


class ArchiveIn(StrictInput):
    expected_updated_at: datetime
    archived: bool = Field(strict=True)
    _aware_timestamp = field_validator("expected_updated_at")(MetadataIn.aware_timestamp.__func__)


class TemplateCreateIn(MutationIn):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    shared: bool = Field(default=False, strict=True)
    acknowledge_shared_content: bool = Field(default=False, strict=True)

    @field_validator("name", "description")
    @classmethod
    def plain_metadata(cls, value, info):
        if "\x00" in value or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("Metadata contains control characters")
        if info.field_name == "name" and not value.strip():
            raise ValueError("A visible template name is required")
        return value.strip()


class TemplatePatchIn(StrictInput):
    expected_revision: int = Field(strict=True, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    shared: bool | None = Field(default=None, strict=True)
    archived: bool | None = Field(default=None, strict=True)
    acknowledge_shared_content: bool = Field(default=False, strict=True)

    @field_validator("name", "description")
    @classmethod
    def plain_metadata(cls, value, info):
        if value is None:
            return value
        return TemplateCreateIn.plain_metadata(value, info)


class TemplateApplyIn(DuplicateIn):
    budget_usd: Decimal = Field(ge=0, le=10000, decimal_places=4)


class PortableProduction(ProductionSettings):
    music_asset_id: None = None
    watermark_asset_id: None = None
    allow_upscale: Literal[False] = False


class PortableSettings(StrictInput):
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    resolution: str = Field(default="1080p", min_length=1, max_length=20)
    language: Literal["en", "zh-CN"] = "en"
    visual_style: str = Field(default="cinematic", max_length=200)
    generation_mode: Literal["text_to_video", "image_to_video"] = "text_to_video"
    default_model_id: str | None = Field(default=None, max_length=160)
    narration_enabled: bool = Field(default=True, strict=True)
    voice: str | None = Field(default=None, max_length=100)
    speaking_rate: Decimal = Field(default=Decimal("1"), ge=Decimal("0.5"), le=2, decimal_places=2)
    target_audience: str = Field(default="", max_length=500)
    production: PortableProduction | None = None


class PortableSource(StrictInput):
    kind: Literal["prompt", "script"]
    text: str = Field(min_length=1, max_length=100000)


class PortableScene(SceneDraft):
    id: None = None
    reference_asset_ids: list[None] = Field(default_factory=list, max_length=0)


class PortableSummary(StrictInput):
    scene_count: int = Field(strict=True, ge=0, le=120)
    target_duration_seconds: int = Field(strict=True, ge=5, le=600)
    language: Literal["en", "zh-CN"]
    model_ids: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(max_length=121)


class PortablePlan(StrictInput):
    schema_version: Literal[1] = 1
    prompt: str = Field(default="", max_length=50000)
    title: str = Field(default="", max_length=200)
    target_duration_seconds: int = Field(strict=True, ge=5, le=600)
    source: PortableSource | None = None
    settings: PortableSettings
    scenes: list[PortableScene] = Field(default_factory=list, max_length=120)
    summary: PortableSummary | None = None

    @model_validator(mode="after")
    def bounded_text_and_order(self):
        texts = [self.prompt, self.title, self.source.text if self.source else ""]
        texts.extend(
            value
            for scene in self.scenes
            for value in (scene.visual_prompt, scene.negative_prompt, scene.narration)
        )
        if any("\x00" in value for value in texts):
            raise ValueError("Plans contain invalid control characters")
        if (
            sum(
                len(s.visual_prompt) + len(s.negative_prompt) + len(s.narration)
                for s in self.scenes
            )
            > 100000
        ):
            raise ValueError("Scene text exceeds the storyboard limit")
        if [s.order for s in self.scenes] != list(range(1, len(self.scenes) + 1)):
            raise ValueError("Scene order must be contiguous")
        if len({s.scene_id for s in self.scenes}) != len(self.scenes):
            raise ValueError("Scene keys must be unique")
        summary = PortableSummary(
            scene_count=len(self.scenes),
            target_duration_seconds=self.target_duration_seconds,
            language=self.settings.language,
            model_ids=sorted(
                {s.video_model_id for s in self.scenes}
                | ({self.settings.default_model_id} if self.settings.default_model_id else set())
            ),
        )
        if self.summary is not None and self.summary != summary:
            raise ValueError("Plan summary does not match its content")
        self.summary = summary
        return self
