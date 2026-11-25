"""Wire shape for strict Structured Outputs; business validation stays outside.

Every model field is required and object extras are forbidden. Duration sums,
model capabilities, stable scene IDs, reference ownership and scalar limits are
validated by the storyboard service before a plan can be saved/approved.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class DirectorScene(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    scene_id: str
    order: int
    duration_seconds: int
    generation_mode: Literal["text_to_video", "image_to_video"]
    video_model_id: str
    resolution: str
    aspect_ratio: str
    audio_enabled: bool
    visual_prompt: str
    negative_prompt: str
    narration: str
    reference_asset_ids: list[str]
    transition: Literal["cut", "fade", "dissolve"]
    status: Literal["draft"]


class DirectorPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    title: str
    scenes: list[DirectorScene]


@dataclass(frozen=True)
class DirectorRequest:
    script: str = field(repr=False)
    target_duration_seconds: int
    language: str
    visual_style: str
    target_audience: str
    default_model_id: str
    budget_usd: Decimal
    models: list[dict]
    reference_assets: list[dict] = field(repr=False)


@dataclass(frozen=True)
class DirectorResult:
    plan: DirectorPlan
    response_id: str | None
    usage: dict


def plan_json_schema() -> dict:
    """Generate schema from the same Pydantic types used to parse the result."""
    schema = DirectorPlan.model_json_schema()

    def compatible(value):
        if isinstance(value, dict):
            # A single-value Literal generates `const`; use the simple enum
            # vocabulary documented for Structured Outputs instead.
            if "const" in value:
                value["enum"] = [value.pop("const")]
            for child in value.values():
                compatible(child)
        elif isinstance(value, list):
            for child in value:
                compatible(child)

    compatible(schema)
    return schema
