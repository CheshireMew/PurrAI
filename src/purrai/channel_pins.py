"""Undo the initial pin of linked-channel forwards, preserving later manual pins."""

from aiogram.exceptions import TelegramBadRequest

from purrai.action_queue import enqueue
from purrai.message_identity import sender_chat
from purrai.models import Group
from purrai.telegram import is_admin


def enabled(group):
    return bool(group and group.approved and group.enabled
                and group.settings.get("linked_channel_auto_unpin", False))


async def observe(session, message, *, edited=False):
    if edited or message.get("chat", {}).get("type") not in ("group", "supergroup"):
        return
    pinned = message.get("pinned_message")
    if not pinned and not message.get("is_automatic_forward"):
        return
    group_id = message["chat"]["id"]
    group = await session.get(Group, group_id)
    if not enabled(group):
        return
    target = pinned or message
    source = sender_chat(target)
    message_id = target.get("message_id")
    if not source or source.get("type") != "channel" or type(message_id) is not int or message_id <= 0:
        return
    if not target.get("is_automatic_forward"):
        return
    if pinned:
        actor = message.get("from") or {}
        anonymous = (message.get("sender_chat") or {}).get("id") == group_id
        human = actor.get("id", 0) > 0 and not actor.get("is_bot") and not message.get("sender_chat")
        if not (anonymous or human):
            return
    action = await enqueue(session, "linked_channel_unpin", f"channel-unpin:{group_id}:{message_id}",
                           group_id=group_id, payload={"message_id": message_id, "channel_id": source["id"]},
                           priority=1)
    # Retain a cancelled intent even when the manual-pin notification arrives
    # before processing the original forward. Replays must not undo that choice.
    if pinned and action.state == "pending":
        action.state, action.error = "cancelled", "保留管理员手动置顶"


class ChannelPinActions:
    def __init__(self, transport):
        self.transport = transport

    async def prepare(self, session, action, group, verification, member):
        if not enabled(group):
            action.state, action.error = "cancelled", "取消关联频道自动置顶已关闭"
            return False
        chat = await self.transport.call(session, action, "get_chat", chat_id=group.id)
        if not isinstance(chat, dict) or chat.get("id") != group.id:
            raise ConnectionError("暂时无法核实本群关联频道")
        if chat.get("linked_chat_id") != action.payload.get("channel_id"):
            action.state, action.error = "cancelled", "消息来源不是本群当前关联频道"
            return False
        rights = await self.transport.call(session, action, "get_chat_member",
                                           chat_id=group.id, user_id=self.transport.tg.user_id)
        if not is_admin(rights) or not (rights.get("can_pin_messages") or rights.get("status") == "creator"):
            raise ValueError("机器人缺少置顶消息权限，无法取消关联频道自动置顶")
        if not enabled(group):
            action.state, action.error = "cancelled", "取消关联频道自动置顶已关闭"
            return False
        return True

    async def unpin(self, session, action, group, verification, member):
        try:
            await self.transport.call(session, action, "unpin_chat_message",
                                      chat_id=group.id, message_id=action.payload["message_id"])
        except TelegramBadRequest as exc:
            if not any(value in str(exc).lower() for value in (
                "message to unpin not found", "message is not pinned", "message_id_invalid",
            )):
                raise
            action.payload = {**action.payload, "already_unpinned": True}
