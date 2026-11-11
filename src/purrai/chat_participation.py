"""Local opening-message and repetition checks, independent of model verdicts."""

import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import aliased

from purrai.chat_retraction import receipt
from purrai.models import ChatMessage, ChatTurn, Meta, now
from purrai.rules import compact, normalize

WINDOW = 24 * 3600
MAX_SEEN = 1000
SHORT_REASON = "独立开场过短或为纯数字"
REPEAT_REASON = "同一成员近期已发送相同内容"


def body(text, cfg, telegram):
    value = normalize(text).strip()
    username = normalize(getattr(telegram, "username", "")).lstrip("@")
    if username:
        value = re.sub(r"@" + re.escape(username) + r"(?![a-z0-9_])", "", value).strip()
    names = {normalize(cfg.chat_name), normalize(getattr(telegram, "display_name", ""))}
    for name in sorted(names - {""}, key=len, reverse=True):
        if value.startswith(name):
            value = value[len(name):].lstrip(" ，,：:。.!！?？")
            break
    return compact(value)


def low_information(text, cfg, telegram):
    if text.startswith(("[贴纸", "[无法读取的图片]")):
        return False
    value = body(text, cfg, telegram)
    return len(value) <= 3 or value.isdecimal()


def fingerprint(topic_id, sender_id, text, cfg, telegram):
    digest = hashlib.sha256(body(text, cfg, telegram).encode()).hexdigest()
    return f"{topic_id}:{sender_id}:{digest}"


async def load(session, group_id):
    row = await session.get(Meta, f"chat_participation:{group_id}")
    try:
        data = json.loads(row.value) if row else {}
        seen = data.get("seen", {})
        seen = {key: value for key, value in seen.items()
                if isinstance(value, dict) and isinstance(value.get("at"), (int, float))
                and value["at"] >= now() - WINDOW and type(value.get("message_id")) is int}
    except (ValueError, TypeError, AttributeError):
        seen = {}
    return row, seen


async def reply_ids(session, group_id, topic_id):
    turns = await session.scalars(select(ChatTurn).where(
        ChatTurn.group_id == group_id, ChatTurn.topic_id == topic_id,
        ChatTurn.created_at >= now() - 6 * 3600, ChatTurn.sent_message_id.is_not(None),
        ChatTurn.state.not_in(("retracted", "retracting", "retraction_failed")),
    ))
    result = set()
    for turn in turns:
        result.add(turn.sent_message_id)
        result.update(value for value in receipt(turn).get("sent_message_ids", []) if type(value) is int)
    return result


async def dialogue_history(session, group_id, topic_id, telegram):
    source, answer = aliased(ChatMessage), aliased(ChatMessage)
    return (await session.execute(select(source.sender_id, source.message_id, answer.received_at).where(
        source.group_id == group_id, source.topic_id == topic_id, source.eligible.is_(True),
        answer.group_id == group_id, answer.topic_id == topic_id, answer.is_bot.is_(True),
        answer.sender_id == telegram.user_id, answer.reply_to_id == source.message_id,
        select(ChatTurn.id).where(ChatTurn.group_id == group_id, ChatTurn.topic_id == topic_id,
            ChatTurn.message_id == source.message_id, ChatTurn.sent_message_id.is_not(None),
            ChatTurn.state.not_in(("retracted", "retracting", "retraction_failed"))).exists(),
    ))).all()


def has_dialogue(history, sender_id, message_id, received_at):
    return bool(received_at and any(user == sender_id and source != message_id
        and received_at - 120 <= answered <= received_at for user, source, answered in history))


async def assess(session, group_id, topic_id, message_id, sender_id, text, cfg, telegram, *, reply_to=0, reference_text="", remember=False):
    if (low_information(text, cfg, telegram) and not (reference_text and not low_information(reference_text, cfg, telegram))
            and reply_to not in await reply_ids(session, group_id, topic_id)
            and not has_dialogue(await dialogue_history(session, group_id, topic_id, telegram), sender_id, message_id, now())):
        return SHORT_REASON
    row, seen = await load(session, group_id)
    key = fingerprint(topic_id, sender_id, text, cfg, telegram)
    previous = seen.get(key)
    if previous and previous["message_id"] != message_id:
        return REPEAT_REASON
    if remember and not previous:
        seen[key] = {"message_id": message_id, "at": now()}
        if len(seen) > MAX_SEEN:
            seen = dict(sorted(seen.items(), key=lambda item: item[1]["at"], reverse=True)[:MAX_SEEN])
        encoded = json.dumps({"seen": seen})
        if row:
            row.value = encoded
        else:
            session.add(Meta(key=f"chat_participation:{group_id}", value=encoded))
    return ""


async def excluded_ids(session, group_id, topic_id, records, cfg, telegram):
    _, seen = await load(session, group_id)
    replies = await reply_ids(session, group_id, topic_id)
    dialogue = await dialogue_history(session, group_id, topic_id, telegram)
    received = dict((await session.execute(select(ChatMessage.message_id, ChatMessage.received_at).where(
        ChatMessage.group_id == group_id, ChatMessage.topic_id == topic_id,
        ChatMessage.message_id.in_([row["id"] for row in records]),
    ))).all())
    excluded = set()
    merged = {}
    for row in records:
        merged[row["id"]] = {**row, **merged.get(row["id"], {})}
    for row in merged.values():
        if row.get("bot"):
            continue
        reference = row.get("quote") or merged.get(row.get("reply_to"), {}).get("text", "")
        meaningful_reference = bool(row.get("image_unavailable") or reference and not low_information(reference, cfg, telegram))
        if (low_information(row["text"], cfg, telegram) and not meaningful_reference and row.get("reply_to") not in replies
                and not has_dialogue(dialogue, row.get("speaker_id"), row["id"], received.get(row["id"]))):
            excluded.add(row["id"])
        key = fingerprint(topic_id, row.get("speaker_id", 0), row["text"], cfg, telegram)
        if key in seen and seen[key]["message_id"] != row["id"]:
            excluded.add(row["id"])
    return excluded
