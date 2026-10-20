"""Persist distinct standalone text contributions before allowing visual media."""

import hashlib
import re

from sqlalchemy import func, select

from purrai.models import ArchivedMessage, Inbox, MemberText, MessageRevision, Meta, Violation
from purrai.rules import compact

KEYCAP = re.compile(r"[0-9#*]\ufe0f?\u20e3")
EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff\u00a9\u00ae\u203c\u2049\u2122\u2139\u3030\u303d\u3297\u3299]"
)
MEDIA = ("photo", "sticker", "animation")
NEW_MEMBER_SECONDS = 15 * 86400


def restricted_newcomer(member, at):
    return bool(
        member
        and member.present
        and member.join_observed
        and 0 < member.joined_at <= at < member.joined_at + NEW_MEMBER_SECONDS
    )


async def completed_text_count(session, group_id, user_id):
    return await session.scalar(
        select(func.count()).select_from(MemberText).where(
            MemberText.group_id == group_id, MemberText.user_id == user_id
        )
    )


def visible_text(message):
    text = message.get("text", "")
    encoded = text.encode("utf-16-le")
    custom = [entity for entity in message.get("entities", []) if entity["type"] == "custom_emoji"]
    for entity in sorted(custom, key=lambda item: item["offset"], reverse=True):
        start, end = entity["offset"] * 2, (entity["offset"] + entity["length"]) * 2
        encoded = encoded[:start] + encoded[end:]
    return KEYCAP.sub("", encoded.decode("utf-16-le"))


def visual_media(message):
    if any(message.get(kind) for kind in MEDIA):
        return True
    if message.get("document", {}).get("mime_type", "").startswith("image/"):
        return True
    text = message.get("text", "")
    has_custom = any(entity["type"] == "custom_emoji" for entity in message.get("entities", []))
    return bool(
        text
        and not compact(visible_text(message))
        and (has_custom or EMOJI.search(text) or KEYCAP.search(text))
    )


def text_digest(message):
    if (
        not message.get("text")
        or message.get("caption")
        or visual_media(message)
        or message.get("sender_chat")
        or message.get("from", {}).get("is_bot")
        or message.get("forward_origin")
        or message.get("via_bot")
        or message["text"].lstrip().startswith("/")
        or any(
            message.get(kind)
            for kind in (
                "video",
                "audio",
                "voice",
                "video_note",
                "document",
                "contact",
                "poll",
                "location",
                "venue",
            )
        )
    ):
        return ""
    value = compact(visible_text(message))
    return hashlib.sha256(value.encode()).hexdigest() if value else ""


async def remember_text(session, message, edited=False):
    if edited:
        return
    digest = text_digest(message)
    if not digest:
        return
    group_id, user_id, message_id = message["chat"]["id"], message["from"]["id"], message["message_id"]
    same_message = await session.get(MemberText, (group_id, user_id, message_id))
    if same_message:
        return
    same_text = await session.scalar(
        select(MemberText).where(
            MemberText.group_id == group_id,
            MemberText.user_id == user_id,
            MemberText.digest == digest,
        )
    )
    if same_text:
        # Delayed originals keep the earliest observed occurrence of that text.
        if message_id < same_text.message_id:
            same_text.message_id = message_id
        return
    session.add(MemberText(group_id=group_id, user_id=user_id, message_id=message_id, digest=digest))
    await session.flush()


async def text_count_before(session, message, minimum):
    group_id, user_id = message["chat"]["id"], message["from"]["id"]

    async def count():
        return await session.scalar(
            select(func.count())
            .select_from(MemberText)
            .where(
                MemberText.group_id == group_id,
                MemberText.user_id == user_id,
                MemberText.message_id < message["message_id"],
            )
        )

    total = await count()
    if total >= minimum:
        return total
    key = f"text-history:{group_id}:{user_id}"
    seeded = await session.get(Meta, key)
    if seeded and int(seeded.value) >= minimum:
        return total
    # Only already-processed originals can provide historical credit. Receipt
    # archiving may already contain later messages from the same polling batch.
    revisions = await session.stream_scalars(
        select(MessageRevision)
        .join(ArchivedMessage, ArchivedMessage.id == MessageRevision.archive_id)
        .join(Inbox, Inbox.id == MessageRevision.update_id)
        .where(
            ArchivedMessage.group_id == group_id,
            ArchivedMessage.sender_id == user_id,
            ArchivedMessage.sender_type == "user",
            ArchivedMessage.message_type == "text",
            ArchivedMessage.source == "telegram",
            ArchivedMessage.message_id < message["message_id"],
            ArchivedMessage.deletion_state == "not_requested",
            MessageRevision.is_edit.is_(False),
            Inbox.state == "done",
            ~ArchivedMessage.message_id.in_(
                select(Violation.message_id).where(Violation.group_id == group_id)
            ),
        )
        .order_by(ArchivedMessage.message_id)
    )
    try:
        async for revision in revisions:
            if revision.reasons:
                continue
            await remember_text(session, revision.payload)
            total = await count()
            if total >= minimum:
                break
    finally:
        await revisions.close()
    if seeded:
        seeded.value = str(minimum)
    else:
        session.add(Meta(key=key, value=str(minimum)))
    return total
