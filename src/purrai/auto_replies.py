"""Match configured replies and queue one durable response per incoming message."""

import unicodedata

from sqlalchemy import select

from purrai.action_delivery import deliver_once
from purrai.action_queue import enqueue
from purrai.chat_state import anonymous_admin, blocked_message, chat_sender
from purrai.models import Action, ArchivedMessage, AutoReply, Group, now
from purrai.record_versions import record_version
from purrai.serialization import row_dict


def normalized(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def matching_reply(text, rules):
    value = normalized(text)
    ordered = sorted(rules, key=lambda item: (-item["priority"], item.get("created_at", 0), item.get("id", "")))
    for rule in ordered:
        keyword = normalized(rule["keyword"])
        if rule["enabled"] and keyword and (value == keyword if rule["mode"] == "exact" else keyword in value):
            return rule
    return None


class AutoReplies:
    def __init__(self, db):
        self.db = db

    async def observe(self, message, *, edited=False):
        chat = message.get("chat", {})
        if chat.get("type") not in ("group", "supergroup"):
            return False
        group_id, message_id = chat["id"], message["message_id"]
        text = message.get("text") or message.get("caption") or ""
        async with self.db.transaction() as session:
            key = f"auto-reply:{group_id}:{message_id}"
            existing = await session.scalar(select(Action).where(Action.dedupe_key == key))
            if existing:
                if edited and existing.state == "pending" and text != existing.payload["source_text"]:
                    existing.state, existing.error = "cancelled", "原消息已编辑，停止旧回复"
                return True
            sender = chat_sender(message)
            if (edited or not text.strip() or text.lstrip().startswith("/") or message.get("via_bot")
                    or not sender.get("id") or sender.get("is_bot") and not anonymous_admin(message)
                    or message.get("sender_chat") and not anonymous_admin(message)
                    or now() - message["date"] > 120):
                return False
            group = await session.get(Group, group_id)
            if not group or not group.approved or await session.scalar(select(blocked_message(group_id, message_id, sender["id"]))):
                return False
            source = await session.scalar(select(ArchivedMessage).where(
                ArchivedMessage.group_id == group_id, ArchivedMessage.message_id == message_id))
            if source and source.latest_is_edit:
                return False
            rules = [row_dict(row) for row in await session.scalars(select(AutoReply).where(
                AutoReply.group_id == group_id, AutoReply.enabled.is_(True)))]
            rule = matching_reply(text, rules)
            if rule is None:
                return False
            action = await enqueue(session, "auto_reply", key, group_id, sender["id"], {
                "rule_id": rule["id"], "rule_version": record_version(rule), "source_text": text,
                "message_id": message_id, "topic_id": message.get("message_thread_id") if message.get("is_topic_message") else 0,
                "expires_at": message["date"] + 120,
            })
            if rule["last_triggered_at"] and now() - rule["last_triggered_at"] < rule["cooldown_seconds"]:
                action.state, action.error = "cancelled", "本条自动回复仍在冷却时间内"
            else:
                row = await session.get(AutoReply, rule["id"])
                row.last_triggered_at = now()
            return True


async def send_reply(call, session, action, group):
    payload = action.payload
    rule = await session.get(AutoReply, payload["rule_id"])
    if (not group or not group.approved or now() > payload["expires_at"] or not rule
            or rule.group_id != group.id or not rule.enabled or record_version(row_dict(rule)) != payload["rule_version"]
            or await session.scalar(select(blocked_message(group.id, payload["message_id"], action.user_id)))):
        action.state, action.error = "cancelled", "规则或原消息状态已变化，停止自动回复"
        return
    check = await session.scalar(select(Action).where(
        Action.dedupe_key == f"advertising-check:{group.id}:{payload['message_id']}"))
    if check and check.state == "pending":
        action.available_at = now() + 1
        return False
    if check and check.state == "failed":
        action.state, action.error = "cancelled", "原消息的广告核查未完成，停止自动回复"
        return
    source = await session.scalar(select(ArchivedMessage).where(
        ArchivedMessage.group_id == action.group_id, ArchivedMessage.message_id == payload["message_id"]))
    if source and (source.text or source.caption) != payload["source_text"]:
        action.state, action.error = "cancelled", "原消息内容已变化，停止自动回复"
        return
    options = {"message_thread_id": payload["topic_id"]} if payload.get("topic_id") else {}
    await deliver_once(call, session, action, "send_message", chat_id=group.id, text=rule.reply,
        parse_mode=None, reply_parameters={"message_id": payload["message_id"], "allow_sending_without_reply": False},
        **options)
