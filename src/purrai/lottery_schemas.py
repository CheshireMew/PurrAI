"""Editable lottery configuration, shared by the bot and management API."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from purrai.points_schemas import MAX_POINTS


class LotteryPrize(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["points", "manual", "none"] = "manual"
    points: int = Field(default=0, strict=True, ge=0, le=1_000_000)
    quantity: int = Field(default=1, strict=True, ge=0, le=100_000)
    weight: int = Field(default=1, strict=True, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def valid_prize(self):
        if self.kind == "points" and not self.points:
            raise ValueError("积分奖品须填写大于零的奖励积分")
        if self.kind != "points" and self.points:
            raise ValueError("只有积分奖品可填写奖励积分")
        if self.kind == "none" and self.quantity or self.kind != "none" and not self.quantity:
            raise ValueError("未中奖项数量须为 0，其他奖品数量须大于 0")
        return self


class LotteryConfig(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    mode: Literal["raffle", "instant"] = "raffle"
    entry_cost: int = Field(default=0, strict=True, ge=0, le=1_000_000)
    min_balance: int = Field(default=0, strict=True, ge=0, le=MAX_POINTS)
    active_window_days: int = Field(default=7, strict=True, ge=1, le=365)
    min_active_days: int = Field(default=0, strict=True, ge=0, le=365)
    draw_mode: Literal["manual", "time", "participants"] = "manual"
    ends_at: float = Field(default=0, ge=0, le=253402271999, allow_inf_nan=False)
    participant_target: int = Field(default=10, strict=True, ge=1, le=100_000)
    min_participants: int = Field(default=1, strict=True, ge=1, le=100_000)
    daily_limit: int = Field(default=1, strict=True, ge=1, le=100)
    prizes: list[LotteryPrize] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def valid_combination(self):
        if self.min_active_days > self.active_window_days:
            raise ValueError("活跃天数不能超过统计天数")
        if self.mode == "instant":
            if not self.entry_cost:
                raise ValueError("即时抽奖须设置每次消耗积分")
            if self.draw_mode == "participants":
                raise ValueError("即时抽奖不使用满人数开奖")
        else:
            if any(p.kind == "none" for p in self.prizes):
                raise ValueError("报名抽奖不需要未中奖奖项")
            if sum(p.quantity for p in self.prizes) > 20:
                raise ValueError("一次统一开奖最多设置 20 个中奖名额")
            if self.draw_mode == "participants" and self.participant_target < self.min_participants:
                raise ValueError("开奖人数不能低于最低成团人数")
        # Drafts copied from a timed activity deliberately have no new timestamp.
        # Publication validates that the selected time is in the future.
        if self.draw_mode != "time" and self.ends_at:
            raise ValueError("只有定时活动可设置结束时间")
        return self


class LotterySave(BaseModel):
    config: LotteryConfig
    version: int = Field(default=0, strict=True, ge=0)


class LotteryOperation(BaseModel):
    operation: Literal["publish", "draw", "cancel", "close", "announce", "fulfill", "copy"]
    version: int = Field(strict=True, ge=1)
    entry_id: str = Field(default="", max_length=32)
