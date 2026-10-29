import time
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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


def now() -> float:
    return time.time()


def uid() -> str:
    return uuid.uuid4().hex


def default_settings() -> dict:
    from purrai.schemas import GroupSettings

    return GroupSettings().model_dump()


class Base(DeclarativeBase):
    pass


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
    penalty: Mapped[str] = mapped_column(String, default="delete")
    inherited_from: Mapped[str] = mapped_column(String, server_default="")
    customized: Mapped[bool] = mapped_column(Boolean, server_default="1")


class Whitelist(Base):
    __tablename__ = "whitelist"
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    verification: Mapped[bool] = mapped_column(Boolean, default=True)
    moderation: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String, default="")


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
    state: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=now)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    response: Mapped[str] = mapped_column(Text, default="")
    sent_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str] = mapped_column(String, default="")


class Inbox(Base):
    __tablename__ = "inbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=now)
    error: Mapped[str] = mapped_column(Text, default="")


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
