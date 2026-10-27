"""Conversation eligibility and durable topic state primitives."""

import json
import re
from contextlib import suppress

from sqlalchemy import func, or_, select

from purrai.models import Action, ArchivedMessage, Meta, Verification, Violation
from purrai.stickers import sticker_text
from purrai.telegram_sources import channel_sources
from purrai.verification_state import ACTIVE as ACTIVE_VERIFICATIONS

FOLLOWUP_WINDOW = 90
IMAGE_BURST_WINDOW = 10
UNREADABLE_IMAGE = "[无法读取的图片]"


def has_image(message):
    return bool(message.get("photo") or (message.get("document") or {}).get("mime_type", "").startswith("image/"))


def message_bursts(rows):
    """Time-connected messages from one sender; no semantic topic guessing."""
    result, latest = [], {}
    for row in sorted(rows, key=lambda item: (item.sent_at, item.message_id)):
        if row.is_bot:
            continue
        key = row.topic_id, row.sender_id
        burst = latest.get(key)
        if burst is None or row.sent_at - burst[-1].sent_at > IMAGE_BURST_WINDOW:
            burst = []
            latest[key] = burst
            result.append(burst)
        burst.append(row)
    return result


def image_message_ids(rows):
    return {
        row.message_id
        for burst in message_bursts(rows)
        if any(item.text.startswith(UNREADABLE_IMAGE) for item in burst)
        for row in burst
    }


def permitted_images(rows, message_id, direct):
    if not direct:
        return set()
    return next((
        {row.message_id for row in burst}
        for burst in message_bursts(rows)
        if any(row.message_id == message_id for row in burst)
    ), set())


def reply_snapshot(message, *, direct=True):
    """Retain source identity for filtering, plus explicit channel/image questions."""
    sources = channel_sources(message)
    metadata = {"channel_sources": sources} if sources else {}
    fallback = json.dumps(metadata, ensure_ascii=False) if metadata else ""
    if not direct:
        return fallback
    reply = message.get("reply_to_message") or {}
    sender = reply.get("sender_chat") or reply.get("from") or {}
    channel = (reply.get("sender_chat") or {}).get("type") == "channel"
    if not reply.get("message_id") or not (channel or has_image(reply)):
        return fallback
    if not channel and (sender.get("is_bot") or reply.get("via_bot")
                        or (reply.get("forward_from") or {}).get("is_bot")
                        or ((reply.get("forward_origin") or {}).get("sender_user") or {}).get("is_bot")):
        return fallback
    text = (reply.get("text") or reply.get("caption") or "")[:1600]
    return json.dumps({**metadata, "version": 1, "message_id": reply["message_id"], "sender_id": sender.get("id", 0),
                       "reference_sources": channel_sources(reply),
                       "text": text, "image_unavailable": has_image(reply)}, ensure_ascii=False)


def following_up(state, sender_id, received_at, *, allow_join=False, message_id=0, burst_ids=()):
    # Classify when the message arrived, so waiting for a worker cannot turn a
    # timely continuation into an unrelated ambient message.
    elapsed = received_at - state.get("last_reply", 0)
    queued_supplement = (
        state.get("last_user") == sender_id
        and message_id > state.get("last_request", 0) > 0
        and state["last_request"] in burst_ids
        and -FOLLOWUP_WINDOW < elapsed < 0
    )
    return (
        (state.get("last_user") == sender_id or allow_join)
        and state.get("last_reply", 0) > 0
        and (0 <= elapsed < FOLLOWUP_WINDOW or queued_supplement)
    )


def ignored_text(text, phrases, bot_username=""):
    # Reply keyboards send ordinary text. Match the whole button label, never a
    # word inside a real question; whitespace and Latin case are insignificant.
    from purrai.chat_commands import candidate, canonical, utility_command

    labels = {canonical(phrase) for phrase in phrases}
    return utility_command(text, bot_username) or canonical(text) in labels or candidate(text) in labels


def human_message(message, ignored_texts=(), *, bot_username=""):
    sender = message.get("from") or {}
    origin = message.get("forward_origin") or {}
    text = sticker_text(message)
    return bool(
        text
        and not text.lstrip().startswith("/")
        and not ignored_text(text, ignored_texts, bot_username)
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
            Action.target_message_id == message_id,
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
