"""Conversation eligibility and durable topic state primitives."""

import json
import re
from contextlib import suppress

from sqlalchemy import func, or_, select

from purrai.models import Action, ArchivedMessage, Meta, Verification, Violation
from purrai.stickers import sticker_text
from purrai.verification_state import ACTIVE as ACTIVE_VERIFICATIONS

FOLLOWUP_WINDOW = 90
REPLY_INTERVAL = 8


def following_up(state, sender_id, received_at):
    # Classify when the message arrived, so waiting for a worker cannot turn a
    # timely continuation into an unrelated ambient message.
    return (
        state.get("last_user") == sender_id
        and received_at - state.get("last_reply", 0) < FOLLOWUP_WINDOW
    )


def human_message(message):
    sender = message.get("from") or {}
    origin = message.get("forward_origin") or {}
    text = sticker_text(message)
    return bool(
        text
        and not text.startswith("/")
        and sender.get("id")
        and not sender.get("is_bot")
        and not message.get("sender_chat")
        and not message.get("via_bot")
        and not message.get("forward_from", {}).get("is_bot")
        and not origin.get("sender_user", {}).get("is_bot")
        and not any(message.get(key) for key in ("new_chat_members", "left_chat_member", "pinned_message"))
    )


def blocked_message(group_id, message_id, sender_id):
    """The same current moderation/deletion facts gate both inputs and history."""
    return or_(
        select(Verification.id)
        .where(
            Verification.group_id == group_id,
            Verification.user_id == sender_id,
            Verification.state.in_(ACTIVE_VERIFICATIONS),
        )
        .exists(),
        select(Violation.id)
        .where(
            Violation.group_id == group_id,
            Violation.message_id == message_id,
        )
        .exists(),
        select(Action.id)
        .where(
            Action.group_id == group_id,
            Action.kind == "delete",
            Action.state != "cancelled",
            func.coalesce(Action.payload["chat_id"].as_integer(), Action.group_id) == group_id,
            Action.payload["message_id"].as_integer() == message_id,
        )
        .exists(),
        select(ArchivedMessage.id)
        .where(
            ArchivedMessage.group_id == group_id,
            ArchivedMessage.message_id == message_id,
            ArchivedMessage.deletion_state != "not_requested",
        )
        .exists(),
    )


def addressed(message, telegram, name):
    text = message.get("text") or message.get("caption") or ""
    username = getattr(telegram, "username", "")
    mention = bool(username and re.search(r"@" + re.escape(username) + r"(?![A-Za-z0-9_])", text, re.I))
    entities = message.get("entities") or message.get("caption_entities") or []
    mention |= any(
        e.get("type") == "text_mention" and e.get("user", {}).get("id") == telegram.user_id for e in entities
    )
    reply = message.get("reply_to_message") or {}
    return (
        mention or name.casefold() in text.casefold() or reply.get("from", {}).get("id") == telegram.user_id
    )


async def session_state(session, group_id, topic_id):
    key = f"chat_session:{group_id}:{topic_id}"
    row = await session.get(Meta, key)
    return row, json.loads(row.value) if row else {}


def has_sent_text(turn):
    with suppress(ValueError, TypeError, AttributeError):
        identifier = json.loads(turn.response).get("text_message_id")
        return type(identifier) is int and identifier > 0
    return False


def store_state(session, row, group_id, topic_id, value):
    encoded = json.dumps(value, ensure_ascii=False)
    if row:
        row.value = encoded
    else:
        session.add(Meta(key=f"chat_session:{group_id}:{topic_id}", value=encoded))
