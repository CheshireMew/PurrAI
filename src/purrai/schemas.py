from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from purrai.pack_names import DEFAULT_BLOCKED_PACKS, emoji_pack_name as pack_name
from purrai.pack_names import DEFAULT_ANTIFRAUD_PACKS, DEFAULT_CHAT_PACKS, sticker_pack_name


class GroupSettings(BaseModel):
    chat_enabled: bool = False
    chat_proactive: bool = True
    chat_name: str = Field(default="柴郡喵", min_length=2, max_length=24)
    chat_persona: str = Field(default="随和、有一点幽默，关心群友，有自己的看法。", max_length=1000)
    chat_ignored_texts: list[str] = Field(
        default_factory=lambda: ["签到", "积分", "抽奖", "注册币安", "注册 OKX", "币圈教程"],
        max_length=100,
    )
    chat_history: int = Field(default=40, ge=10, le=80)
    chat_interval: int = Field(default=90, ge=15, le=3600)
    chat_stickers: bool = True
    chat_sticker_packs: list[str] = Field(default_factory=lambda: list(DEFAULT_CHAT_PACKS), max_length=50)
    chat_sticker_interval: int = Field(default=60, ge=15, le=86400)
    chat_antifraud: bool = True
    chat_antifraud_packs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ANTIFRAUD_PACKS), max_length=20
    )
    chat_antifraud_interval: int = Field(default=21600, ge=3600, le=604800)
    archive_enabled: bool = True
    join_requests: bool = False
    verification_enabled: bool = True
    verification_timeout: int = Field(default=180, ge=30, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    verification_failure_action: Literal["kick", "permanent_mute"] = "permanent_mute"
    # Legacy key retained for explicit manual bans and join-request retry intervals.
    failure_ban_seconds: int = Field(default=600, ge=30, le=86400)
    newcomer_seconds: int = Field(default=600, ge=0, le=86400)
    newcomer_links: bool = True
    newcomer_contacts: bool = True  # Legacy setting; contact sharing now always removes ordinary members.
    newcomer_messages: int = Field(default=3, ge=0, le=100)
    media_min_text_messages: int = Field(default=3, ge=0, le=100)
    emoji_status_blocked_packs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BLOCKED_PACKS), max_length=100
    )
    newcomer_mentions: bool = True
    repeat_enabled: bool = True
    repeat_window: int = Field(default=30, ge=5, le=3600)
    repeat_count: int = Field(default=5, ge=2, le=100)
    similar_enabled: bool = True
    similar_window: int = Field(default=60, ge=5, le=3600)
    similar_count: int = Field(default=4, ge=2, le=100)
    similar_threshold: int = Field(default=85, ge=70, le=100)
    similar_min_length: int = Field(default=12, ge=8, le=200)
    burst_enabled: bool = True
    burst_window: int = Field(default=10, ge=2, le=300)
    burst_count: int = Field(default=8, ge=3, le=100)
    warning_window: int = Field(default=86400, ge=60, le=604800)
    warning_limit: int = Field(default=3, ge=1, le=100)
    mute_seconds: int = Field(default=259200, ge=30, le=2592000)
    block_invites: bool = False

    @field_validator("chat_name")
    @classmethod
    def chat_name_not_blank(cls, value):
        value = value.strip()
        if len(value) < 2 or "\n" in value:
            raise ValueError("聊天昵称请填写 2 至 24 个字符，不能换行")
        return value

    @field_validator("chat_ignored_texts")
    @classmethod
    def normalize_chat_ignored_texts(cls, value):
        phrases = {}
        for item in value:
            item = item.strip()
            if not item:
                continue
            if len(item) > 80 or "\n" in item or "\r" in item:
                raise ValueError("聊天忽略口令每条最多 80 个字符，不能换行")
            phrases.setdefault("".join(item.split()).casefold(), item)
        return list(phrases.values())

    @field_validator("emoji_status_blocked_packs")
    @classmethod
    def normalize_emoji_packs(cls, value):
        return list(dict.fromkeys(pack_name(item) for item in value))

    @field_validator("chat_sticker_packs", "chat_antifraud_packs")
    @classmethod
    def normalize_sticker_packs(cls, value):
        names = {}
        for item in value:
            name = sticker_pack_name(item)
            names.setdefault(name.lower(), name)
        return list(names.values())


class SettingsPatch(BaseModel):
    enabled: bool | None = None
    settings: dict | None = None
    baseline: dict | None = None

    @field_validator("settings", "baseline")
    @classmethod
    def validate_changes(cls, value):
        if value is None:
            return None
        unknown = set(value) - GroupSettings.model_fields.keys()
        if unknown:
            raise ValueError("未知群设置：" + "、".join(sorted(unknown)))
        return GroupSettings.model_validate(value).model_dump(include=set(value))


class EmojiPackChange(BaseModel):
    operation: Literal["add", "remove", "clear"] = "add"
    packs: list[str] = Field(default_factory=list, max_length=100)
    request_id: str = Field(min_length=8, max_length=80)

    @field_validator("packs")
    @classmethod
    def normalize_packs(cls, value):
        return list(dict.fromkeys(pack_name(item) for item in value))

    @model_validator(mode="after")
    def require_target(self):
        if self.operation != "clear" and not self.packs:
            raise ValueError("请填写要新增或移除的表情包链接")
        if self.operation == "clear" and self.packs:
            raise ValueError("清空名单不需要填写表情包")
        return self


class GroupAccessInput(BaseModel):
    approved: bool


class RuleInput(BaseModel):
    kind: Literal["keyword", "block_domain", "allow_domain", "block_mention", "allow_mention"]
    value: str = Field(min_length=1, max_length=250)
    mode: Literal["contains", "exact", "compact", "pinyin"] = "contains"
    scope: Literal["message", "name", "either", "name_and_message"] = "message"
    context_value: str = Field(default="", max_length=250)
    penalty: Literal["delete", "permanent_mute"] = "delete"
    enabled: bool = True

    @field_validator("value")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("规则不能为空")
        return value.strip()

    @model_validator(mode="after")
    def safe_matching(self):
        if self.kind == "keyword":
            if self.scope == "name_and_message" and not self.context_value.strip():
                raise ValueError("组合规则需要同时填写昵称关键词和正文关键词")
            if self.mode == "pinyin":
                if len(self.value.strip()) < 2 or (
                    self.scope == "name_and_message" and len(self.context_value.strip()) < 2
                ):
                    raise ValueError("谐音规则请至少填写两个字的词组")
        return self


class PreviewInput(BaseModel):
    text: str = Field(max_length=10000)
    newcomer: bool = False
    contact: bool = False
    repeats: int = Field(default=0, ge=0)
    similar: int = Field(default=0, ge=0)
    burst: int = Field(default=0, ge=0)
    sender_name: str = Field(default="", max_length=500)
    rules: list[RuleInput] | None = None


class WhitelistInput(BaseModel):
    user_id: int = Field(gt=0)
    verification: bool = True
    moderation: bool = True
    note: str = Field(default="", max_length=300)


class AdminInput(BaseModel):
    id: int = Field(gt=0)
    name: str = Field(default="", max_length=100)
    active: bool = True
    group_ids: list[int] = Field(default_factory=list)


class ActionInput(BaseModel):
    action: Literal[
        "release",
        "reissue",
        "mute",
        "ban",
        "unmute",
        "unban",
        "revoke_warning",
    ]
    reason: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=8, max_length=80)


class ArchiveFilter(BaseModel):
    query: str = Field(default="", max_length=250)
    sender_id: int | None = None
    after: float = Field(default=0, ge=0)
    before: float = Field(default=0, ge=0)
    message_type: str = Field(default="", max_length=40)
    flagged: bool = False
    deletion: Literal["", "deleted", "already_absent", "pending", "failed", "not_requested"] = ""

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.before and self.after > self.before:
            raise ValueError("开始时间不能晚于结束时间")
        return self


class ArchiveQuery(ArchiveFilter):
    page_number: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)
