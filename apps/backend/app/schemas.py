from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    role: str


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    prompt: str = Field(default="", max_length=50000)
    target_duration_seconds: int = Field(default=15, ge=5, le=600)
    budget_usd: Decimal = Field(default=Decimal("10"), ge=0, le=10000, decimal_places=4)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    language: Literal["en", "zh-CN"] = "en"


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    owner_id: UUID
    name: str
    prompt: str
    status: str
    target_duration_seconds: int
    budget_usd: Decimal
    approved_cap_usd: Decimal | None
    committed_cost_usd: Decimal
    archived: bool = False
    created_at: datetime
    updated_at: datetime
