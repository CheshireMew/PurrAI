import time
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from purrai.business_rules import default_business_rules


def now() -> float:
    return time.time()


def uid() -> str:
    return uuid.uuid4().hex


def default_settings() -> dict:
    from purrai.schemas import GroupSettings

    return GroupSettings().model_dump()


class Base(DeclarativeBase):
    pass


class PointsAccount(Base):
    __tablename__ = "points_accounts"
    __table_args__ = (UniqueConstraint("group_id", "source_key"), UniqueConstraint("group_id", "user_id"),
                      CheckConstraint("balance >= 0 AND balance <= 9007199254740991"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    source_key: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    source_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class PointsImport(Base):
    __tablename__ = "points_imports"
    __table_args__ = (UniqueConstraint("group_id", "batch_key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    batch_key: Mapped[str] = mapped_column(String)
    digest: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(Text)
    count: Mapped[int] = mapped_column(Integer)
    total_points: Mapped[int] = mapped_column(BigInteger)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class PointsEntry(Base):
    __tablename__ = "points_entries"
    __table_args__ = (UniqueConstraint("group_id", "request_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("points_accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    delta: Mapped[int] = mapped_column(BigInteger)
    balance_after: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    request_id: Mapped[str] = mapped_column(String)
    intent: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class PointsPolicy(Base):
    __tablename__ = "points_policies"
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)


class PointsDaily(Base):
    __tablename__ = "points_daily"
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[str] = mapped_column(String, primary_key=True)
    signed: Mapped[bool] = mapped_column(Boolean, default=False)
    chat_points: Mapped[int] = mapped_column(Integer, default=0)
    last_chat_at: Mapped[float] = mapped_column(Float, default=0)
    last_digest: Mapped[str] = mapped_column(String, default="")


class PointsPacket(Base):
    __tablename__ = "points_packets"
    __table_args__ = (UniqueConstraint("group_id", "message_id"),
                     CheckConstraint("remaining >= 0 AND remaining_count >= 0"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    sender_id: Mapped[int] = mapped_column(BigInteger)
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    total: Mapped[int] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(Integer)
    remaining: Mapped[int] = mapped_column(BigInteger)
    remaining_count: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    refunded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class PointsClaim(Base):
    __tablename__ = "points_claims"
    packet_id: Mapped[str] = mapped_column(ForeignKey("points_packets.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    points: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Lottery(Base):
    __tablename__ = "lotteries"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    config: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String, default="draft", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    participant_count: Mapped[int] = mapped_column(Integer, default=0)
    draw_count: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    opened_at: Mapped[float] = mapped_column(Float, default=0)
    finished_at: Mapped[float] = mapped_column(Float, default=0)


class LotteryEntry(Base):
    __tablename__ = "lottery_entries"
    __table_args__ = (UniqueConstraint("lottery_id", "request_id"),
                     Index("ix_lottery_entry_user", "lottery_id", "user_id", "day"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    lottery_id: Mapped[str] = mapped_column(ForeignKey("lotteries.id"), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    display_name: Mapped[str] = mapped_column(String)
    request_id: Mapped[str] = mapped_column(String)
    day: Mapped[str] = mapped_column(String)
    fee: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String, default="joined", index=True)
    prize_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prize_label: Mapped[str] = mapped_column(String, default="")
    reward_points: Mapped[int] = mapped_column(BigInteger, default=0)
    fulfilled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String, default="")
    type: Mapped[str] = mapped_column(String, default="supergroup")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    settings: Mapped[dict] = mapped_column(JSON, default=default_settings)
    setting_overrides: Mapped[list] = mapped_column(JSON, server_default="[]")
    activated_at: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class SharedPolicy(Base):
    __tablename__ = "shared_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    settings: Mapped[dict] = mapped_column(JSON, default=default_settings)
    rules: Mapped[list] = mapped_column(JSON, default=list)
    business_rules: Mapped[dict] = mapped_column(JSON, default=default_business_rules)


class PromotionSource(Base):
    __tablename__ = "promotion_sources"
    rule_id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    violation_id: Mapped[str] = mapped_column(String, default="", index=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    withdrawn_at: Mapped[float] = mapped_column(Float, default=0)


class RuleHit(Base):
    __tablename__ = "rule_hits"
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_id: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Grant(Base):
    __tablename__ = "grants"
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), primary_key=True)


class Member(Base):
    __tablename__ = "members"
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    joined_at: Mapped[float] = mapped_column(Float, default=now)
    join_observed: Mapped[bool] = mapped_column(Boolean, default=False)
    passed_at: Mapped[float] = mapped_column(Float, default=0)
    generation: Mapped[str] = mapped_column(String, default=uid)
    owned_restriction: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    restriction_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    last_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    observed_messages: Mapped[int] = mapped_column(Integer, default=0)
    request_until: Mapped[float] = mapped_column(Float, default=0)


class Verification(Base):
    __tablename__ = "verifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_name: Mapped[str] = mapped_column(String, default="")
    generation: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="muting", index=True)
    nonce: Mapped[str] = mapped_column(String, default=lambda: uuid.uuid4().hex[:8])
    answer_hash: Mapped[str] = mapped_column(String, default="")
    input_digits: Mapped[str] = mapped_column(String, default="")
    choices: Mapped[list[str]] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deadline: Mapped[float] = mapped_column(Float)
    previous_permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)
    flow: Mapped[str] = mapped_column(String, default="joined")
    prompt_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    request_date: Mapped[float] = mapped_column(Float, default=0)
    failure_action: Mapped[str] = mapped_column(String, default="")
    method: Mapped[str] = mapped_column(String, default="image", server_default="image")
    challenge: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class Rule(Base):
    __tablename__ = "rules"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String, default="contains")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    scope: Mapped[str] = mapped_column(String, default="message")
    context_value: Mapped[str] = mapped_column(String, default="")
    meaning: Mapped[str] = mapped_column(String, default="", server_default="")
    penalty: Mapped[str] = mapped_column(String, default="delete")
    name_penalty: Mapped[str] = mapped_column(String, default="permanent_mute", server_default="permanent_mute")
    inherited_from: Mapped[str] = mapped_column(String, server_default="")
    customized: Mapped[bool] = mapped_column(Boolean, server_default="1")


class AutoReply(Base):
    __tablename__ = "auto_replies"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    keyword: Mapped[str] = mapped_column(String)
    reply: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String, default="exact")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=10)
    last_triggered_at: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Whitelist(Base):
    __tablename__ = "whitelist"
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    verification: Mapped[bool] = mapped_column(Boolean, default=True)
    moderation: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String, default="")
    administrator_backup: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Violation(Base):
    __tablename__ = "violations"
    __table_args__ = (UniqueConstraint("group_id", "message_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    sender_chat: Mapped[bool] = mapped_column(Boolean, default=False)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    reason_details: Mapped[dict] = mapped_column(JSON, default=dict)
    warning_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    sender_name: Mapped[str] = mapped_column(String, default="")
    profile_violation: Mapped[bool] = mapped_column(Boolean, default=False)
    rule_mute: Mapped[bool] = mapped_column(Boolean, default=False)
    message_violation: Mapped[bool] = mapped_column(Boolean, default=True)


class Fingerprint(Base):
    __tablename__ = "fingerprints"
    __table_args__ = (
        Index("ix_fingerprints_member_window", "group_id", "user_id", "generation", "created_at", "message_id"),
        Index("ix_fingerprints_batch", "group_id", "user_id", "generation", "batch_key"),
    )
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    generation: Mapped[str] = mapped_column(String, default="")
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    batch_key: Mapped[str] = mapped_column(String, default="")
    similarity_hash: Mapped[str] = mapped_column(String, default="")
    similarity_length: Mapped[int] = mapped_column(Integer, default=0)


class MemberText(Base):
    __tablename__ = "member_texts"
    __table_args__ = (UniqueConstraint("group_id", "user_id", "digest"),)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest: Mapped[str] = mapped_column(String)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class ArchivedMessage(Base):
    __tablename__ = "archived_messages"
    __table_args__ = (
        UniqueConstraint("group_id", "message_id"),
        Index("ix_archive_group_date", "group_id", "sent_at", "message_id"),
        Index("ix_archive_sender_date", "group_id", "sender_id", "sent_at"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    message_id: Mapped[int] = mapped_column(BigInteger)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_type: Mapped[str] = mapped_column(String, default="user")
    sender_name: Mapped[str] = mapped_column(String, default="")
    username: Mapped[str] = mapped_column(String, default="")
    message_type: Mapped[str] = mapped_column(String, default="text")
    text: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[float] = mapped_column(Float)
    edited_at: Mapped[float] = mapped_column(Float, default=0)
    latest_update_id: Mapped[int] = mapped_column(BigInteger, default=0)
    latest_is_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    original_observed: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String, default="telegram")
    revision_count: Mapped[int] = mapped_column(Integer, default=0)
    deletion_state: Mapped[str] = mapped_column(String, default="not_requested", index=True)
    deletion_error: Mapped[str] = mapped_column(Text, default="")
    deletion_action_id: Mapped[str] = mapped_column(String, default="")
    deleted_at: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class MessageRevision(Base):
    __tablename__ = "message_revisions"
    __table_args__ = (UniqueConstraint("archive_id", "version_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    archive_id: Mapped[str] = mapped_column(ForeignKey("archived_messages.id"), index=True)
    version_key: Mapped[str] = mapped_column(String)
    update_id: Mapped[int] = mapped_column(BigInteger, default=0)
    is_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    event_at: Mapped[float] = mapped_column(Float)
    received_at: Mapped[float] = mapped_column(Float, default=now)
    payload: Mapped[dict] = mapped_column(JSON)
    reasons: Mapped[list] = mapped_column(JSON, default=list)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("group_id", "message_id"),
        Index("ix_chat_context", "group_id", "topic_id", "sent_at", "message_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    topic_id: Mapped[int] = mapped_column(BigInteger, default=0)
    message_id: Mapped[int] = mapped_column(BigInteger)
    sender_id: Mapped[int] = mapped_column(BigInteger)
    sender_name: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    reply_to_id: Mapped[int] = mapped_column(BigInteger, default=0)
    reply_text: Mapped[str] = mapped_column(Text, default="")
    edited_at: Mapped[float] = mapped_column(Float, default=0)
    update_id: Mapped[int] = mapped_column(BigInteger, default=0)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    sent_at: Mapped[float] = mapped_column(Float)
    received_at: Mapped[float] = mapped_column(Float, default=now)


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        UniqueConstraint("group_id", "message_id"),
        Index("ix_chat_pending", "state", "available_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    topic_id: Mapped[int] = mapped_column(BigInteger, default=0)
    message_id: Mapped[int] = mapped_column(BigInteger)
    direct: Mapped[bool] = mapped_column(Boolean, default=False)
    review_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    state: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=now)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    response: Mapped[str] = mapped_column(Text, default="")
    sent_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str] = mapped_column(String, default="")


class AdvertisementPolicy(Base):
    __tablename__ = "advertisement_policies"
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class AdvertisementSample(Base):
    __tablename__ = "advertisement_samples"
    __table_args__ = (UniqueConstraint("group_id", "source_key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    sender_name: Mapped[str] = mapped_column(String, default="")
    scope: Mapped[str] = mapped_column(String, default="context")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String)
    source_key: Mapped[str] = mapped_column(String)
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    violation_id: Mapped[str] = mapped_column(String, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)
    review_state: Mapped[str] = mapped_column(String, default="done")
    review_attempts: Mapped[int] = mapped_column(Integer, default=0)
    review_after: Mapped[float] = mapped_column(Float, default=0)
    review_error: Mapped[str] = mapped_column(Text, default="")


class AdvertisementReview(Base):
    __tablename__ = "advertisement_reviews"
    __table_args__ = (
        UniqueConstraint("group_id", "message_id", "revision"),
        Index("ix_ad_review_queue", "state", "available_at"),
        Index("ix_ad_review_local", "state", "local_after"),
        Index("ix_ad_review_message", "group_id", "message_id"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    message_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[str] = mapped_column(String)
    message: Mapped[dict] = mapped_column(JSON)
    event_at: Mapped[float] = mapped_column(Float)
    update_id: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String, default="pending")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=now)
    local_after: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)
    error: Mapped[str] = mapped_column(Text, default="")


class Inbox(Base):
    __tablename__ = "inbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=now)
    received_at: Mapped[float] = mapped_column(Float, default=now, server_default="0")
    error: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[float] = mapped_column(Float, default=0, server_default="0", index=True)


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (Index("ix_actions_batch_state", "batch_id", "state"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    dedupe_key: Mapped[str] = mapped_column(String, unique=True)
    batch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String, default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=now)
    error: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)

    @hybrid_property
    def target_message_id(self):
        return self.payload.get("message_id")

    @target_message_id.expression
    def target_message_id(cls):
        # Keep this JSON path literal: a bound path cannot use SQLite's
        # expression index, even when its runtime value is identical.
        return func.json_extract(cls.payload, literal_column("'$.message_id'"))


Index("ix_actions_message", Action.group_id, Action.kind, Action.target_message_id)
Index("ix_actions_member_pending", Action.group_id, Action.user_id, Action.state)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    csrf: Mapped[str] = mapped_column(String)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    state: Mapped[str] = mapped_column(String, primary_key=True)
    nonce: Mapped[str] = mapped_column(String)
    verifier: Mapped[str] = mapped_column(String)
    expires_at: Mapped[float] = mapped_column(Float)


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float, default=0, server_default="0", index=True)
