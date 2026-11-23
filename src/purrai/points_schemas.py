"""Validated points data; nicknames are never identity keys."""
from typing import Annotated

from pydantic import BaseModel, Field, ConfigDict, model_validator

MAX_POINTS = 9_007_199_254_740_991


class PointsModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class ImportRow(PointsModel):
    source_key: str = Field(min_length=1, max_length=160)
    display_name: str = Field(max_length=250)
    user_id: int | None = Field(default=None, strict=True, ge=1, le=MAX_POINTS)
    points: int = Field(strict=True, ge=0, le=MAX_POINTS)
    source_rank: int | None = Field(default=None, strict=True, ge=1)
    note: str = Field(default="", max_length=1000)


class PointsImportInput(PointsModel):
    batch_key: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=2000)
    rows: list[ImportRow] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_sources(self):
        if len({row.source_key for row in self.rows}) != len(self.rows):
            raise ValueError("来源编号重复，请先去重；同名成员应使用不同来源编号")
        users = [row.user_id for row in self.rows if row.user_id is not None]
        if len(set(users)) != len(users):
            raise ValueError("Telegram ID 重复，请核对来源，不能把多个排名记给同一个人")
        if sum(row.points for row in self.rows) > MAX_POINTS:
            raise ValueError("本批积分总额过大")
        return self


class PointsAdjustment(PointsModel):
    delta: int = Field(strict=True, ge=-MAX_POINTS, le=MAX_POINTS)
    reason: str = Field(min_length=1, max_length=1000)
    version: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def nonzero(self):
        if self.delta == 0:
            raise ValueError("增减积分不能为零")
        return self


class PointsIdentity(PointsModel):
    user_id: int | None = Field(default=None, strict=True, ge=1, le=MAX_POINTS)
    display_name: str = Field(min_length=1, max_length=250)
    reason: str = Field(min_length=1, max_length=1000)
    version: int = Field(strict=True, ge=1)


class PointsCreate(PointsModel):
    user_id: int = Field(strict=True, ge=1, le=MAX_POINTS)
    display_name: str = Field(min_length=1, max_length=250)


class PointsSettings(PointsModel):
    enabled: bool = False
    signin_points: int = Field(default=10, strict=True, ge=0, le=10000)
    chat_points: int = Field(default=1, strict=True, ge=0, le=100)
    chat_daily_limit: int = Field(default=50, strict=True, ge=0, le=10000)
    chat_min_characters: int = Field(default=3, strict=True, ge=1, le=100)
    chat_interval_seconds: int = Field(default=60, strict=True, ge=10, le=3600)
    packet_enabled: bool = True
    packet_expiry_hours: int = Field(default=24, strict=True, ge=1, le=168)
    trust_enabled: bool = True
    newcomer_exemption_points: int = Field(default=50, strict=True, ge=1, le=MAX_POINTS)
    advertising_exemption_points: int = Field(default=100, strict=True, ge=1, le=MAX_POINTS)
    trust_excluded_user_ids: list[Annotated[int, Field(strict=True, ge=1, le=MAX_POINTS)]] = Field(default_factory=list, max_length=500)
    commands: dict[str, list[str]]

    @model_validator(mode="after")
    def valid_trust(self):
        if self.advertising_exemption_points < self.newcomer_exemption_points:
            raise ValueError("广告审核豁免门槛应不低于新人限制豁免门槛")
        self.trust_excluded_user_ids = list(dict.fromkeys(self.trust_excluded_user_ids))
        return self

    @model_validator(mode="after")
    def valid_commands(self):
        import unicodedata
        required = {"signin", "balance", "ranking", "packet"}
        optional = {"add", "subtract", "lookup"}
        if not required <= set(self.commands) or set(self.commands) - required - optional:
            raise ValueError("需要签到、余额、排行和红包口令，可配置增减积分和回复查询口令")
        self.commands = {**dict.fromkeys(optional, []), **self.commands}
        seen = set()
        for aliases in self.commands.values():
            if len(aliases) > 10:
                raise ValueError("每组最多 10 个口令")
            for phrase in aliases:
                key = unicodedata.normalize("NFKC", phrase).strip().casefold()
                if not key or len(key) > 30 or any(c.isspace() for c in key) or key in seen:
                    raise ValueError("口令须为不含空格的 1–30 个字符，且不能重复")
                seen.add(key)
        return self


class PointsSettingsInput(PointsModel):
    config: PointsSettings
    version: int = Field(strict=True, ge=0)
