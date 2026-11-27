"""Editable sample and review configuration; no advertising vocabulary."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LOCAL_REVIEW_WINDOW_SECONDS = 47 * 3600


class AdvertisementSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(default="", exclude=True)
    enabled: bool = True
    similarity_enabled: bool = True
    threshold: float = Field(default=0.90, ge=0.75, le=1)
    minimum_length: int = Field(default=14, ge=8, le=200)
    minimum_sample_coverage: float = Field(default=0.55, ge=0.4, le=1)
    sample_action: Literal["delete", "permanent_mute"] = "delete"
    batch_size: int = Field(default=12, ge=1, le=24)
    context_messages: int = Field(default=5, ge=0, le=10)
    context_window_seconds: int = Field(default=900, ge=30, le=3600)


class AdvertisementSampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(default="", exclude=True)
    text: str = Field(min_length=1, max_length=4096)
    sender_name: str = Field(default="", max_length=500)
    scope: Literal["text", "bio", "context"] = "text"
    enabled: bool = True
    reason: str = Field(min_length=1, max_length=500)


class AdvertisementPreview(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    sender_name: str = Field(default="", max_length=500)
    quoted: bool = False
    scope: Literal["text", "bio"] = "text"


class AdvertisementImport(BaseModel):
    apply: bool = False
    # Historical verdicts did not distinguish message text from profile/context.
    # Import preserves them for review; only an explicit confirmation enables text reuse.


class AdvertisementRetry(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
