"""Editable keyword replies. All trigger words and responses are user data."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AutoReplyInput(BaseModel):
    keyword: str = Field(min_length=1, max_length=250)
    reply: str = Field(min_length=1, max_length=4000)
    mode: Literal["exact", "contains"] = "exact"
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=1000)
    cooldown_seconds: int = Field(default=10, ge=0, le=86400)

    @field_validator("keyword", "reply")
    @classmethod
    def nonempty(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("关键词和回复内容不能为空")
        if len(value.encode("utf-16-le")) // 2 > 4000:
            raise ValueError("回复内容不能超过 4000 个字符，表情可能占两个字符")
        return value


class AutoReplyPreview(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    draft: AutoReplyInput | None = None
