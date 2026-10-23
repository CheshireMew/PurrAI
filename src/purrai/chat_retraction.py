"""Durable reply receipts and retraction when a source deletion is confirmed."""

import json

from sqlalchemy import case, func, or_, select

from purrai.models import Action, ArchivedMessage, ChatMessage, ChatTurn, now

# Telegram only permits deleting messages sent less than 48 hours ago. Allow
# the existing two-minute generation lifetime before forgetting a turn.
RECEIPT_RETENTION = 48 * 3600 + 120


def receipt(turn):
    try:
        value = json.loads(turn.response)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def finish_receipt(turn):
    value = receipt(turn)
    turn.response = json.dumps({key: value[key] for key in ("sent_message_ids", "source_ids") if key in value})


async def source_deleted(session, group_id, message_id):
    return bool(await session.scalar(select(or_(
        select(ArchivedMessage.id).where(
            ArchivedMessage.group_id == group_id, ArchivedMessage.message_id == message_id,
            ArchivedMessage.deletion_state.in_(("deleted", "already_absent")),
        ).exists(),
        select(Action.id).where(
            Action.group_id == group_id, Action.kind == "delete", Action.state == "succeeded",
            Action.payload["message_id"].as_integer() == message_id,
            or_(Action.payload["chat_id"].as_integer().is_(None), Action.payload["chat_id"].as_integer() == group_id),
        ).exists(),
    ))))


async def retract_replies(session, group_id, message_id):
    from purrai.action_queue import enqueue

    sources = func.json_each(case((func.json_valid(ChatTurn.response), ChatTurn.response), else_="{}"),
                             "$.source_ids").table_valued("value")
    turns = (await session.scalars(select(ChatTurn).where(
        ChatTurn.group_id == group_id, ChatTurn.sent_message_id.is_not(None),
        ChatTurn.created_at >= now() - RECEIPT_RETENTION,
        or_(ChatTurn.message_id == message_id,
            select(sources.c.value).where(sources.c.value == message_id).correlate(ChatTurn).exists()),
    ))).all()
    for turn in turns:
        saved = receipt(turn)
        if turn.message_id != message_id and message_id not in saved.get("source_ids", []):
            continue
        # Legacy releases kept only the last receipt. The small rolling window
        # can recover a preceding text receipt without requiring the archive.
        identifiers = set(saved.get("sent_message_ids", [])) | {turn.sent_message_id}
        identifiers.update(await session.scalars(select(ChatMessage.message_id).where(
            ChatMessage.group_id == group_id, ChatMessage.is_bot.is_(True),
            ChatMessage.reply_to_id == turn.message_id,
        )))
        for identifier in sorted(identifiers):
            await enqueue(session, "delete", f"cleanup:chat-reply:{group_id}:{identifier}", group_id,
                          payload={"message_id": identifier, "chat_turn_id": turn.id}, priority=0,
                          reason="原消息已确认删除，同步撤回机器人回复")
        turn.state, turn.error = "retracting", "原消息已删除，正在撤回已发送回复"
        saved["sent_message_ids"] = sorted(identifiers)
        turn.response = json.dumps(saved)
        finish_receipt(turn)
        await retraction_result(session, turn.id)


async def retract_if_deleted(session, turn):
    for identifier in set(receipt(turn).get("source_ids", [])) | {turn.message_id}:
        if await source_deleted(session, turn.group_id, identifier):
            await retract_replies(session, turn.group_id, identifier)
            return True
    return False


async def retraction_result(session, turn_id):
    turn = await session.get(ChatTurn, turn_id)
    if not turn:
        return
    identifiers = receipt(turn).get("sent_message_ids", [])
    if not identifiers:
        return
    keys = [f"cleanup:chat-reply:{turn.group_id}:{identifier}" for identifier in identifiers]
    actions = (await session.scalars(select(Action).where(Action.dedupe_key.in_(keys)))).all()
    if len(actions) == len(keys) and all(action.state == "succeeded" for action in actions):
        turn.state, turn.error = "retracted", "原消息已删除，关联回复已撤回"
    elif any(action.state in ("failed", "cancelled") for action in actions):
        turn.state, turn.error = "retraction_failed", "原消息已删除，部分回复撤回失败；请检查对应删除任务"


async def remember_receipt(session, turn, message_id, source_ids):
    saved = receipt(turn)
    saved["sent_message_ids"] = sorted(set(saved.get("sent_message_ids", [])) | {message_id})
    saved["source_ids"] = sorted(set(saved.get("source_ids", [])) | set(source_ids) | {turn.message_id})
    turn.response = json.dumps(saved)
    await retract_if_deleted(session, turn)
