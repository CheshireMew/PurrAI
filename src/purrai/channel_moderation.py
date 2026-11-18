"""Review chat identities without treating Telegram's compatibility user as their owner."""

from purrai.action_execution import ActionSuperseded
from purrai.action_queue import enqueue
from purrai.group_link_identity import own_channel_id, remember_linked_channel
from purrai.message_identity import sender_chat
from purrai.schemas import GroupSettings
from purrai.models import Action, Group
from purrai.errors import ManagementError
from sqlalchemy import select


async def trusted(session, group_id, message):
    sender = sender_chat(message)
    return bool(sender and sender["id"] in (group_id, await own_channel_id(session, group_id)))


async def refresh(db, telegram, message):
    sender = sender_chat(message)
    group_id = message.get("chat", {}).get("id")
    if not sender or sender["id"] == group_id:
        return
    async with db.sessions() as session:
        group = await session.get(Group, group_id)
        if not group or not group.approved or not group.enabled:
            return
    chat = await telegram.call("get_chat", chat_id=group_id)
    if not isinstance(chat, dict) or chat.get("id") != group_id:
        raise ConnectionError("暂时无法核实本群关联频道，稍后重新审核")
    async with db.transaction() as session:
        await remember_linked_channel(session, group_id, chat)


async def queue_ban(session, group, user_id, key, payload):
    if GroupSettings.model_validate(group.settings).channel_ad_penalty != "ban":
        return None
    return await enqueue(session, "channel_ad_ban", key, group.id, user_id,
                         {**payload, "channel_evidence": {"sender_chat_id": user_id}},
                         priority=1, reason="频道身份广告，禁止该频道继续在群内发言")


async def latest_ban(session, group_id, sender_id):
    latest = await session.scalar(select(Action).where(
        Action.group_id == group_id, Action.user_id == sender_id,
        Action.kind.in_(("channel_ad_ban", "channel_unban")), Action.state == "succeeded",
    ).order_by(Action.updated_at.desc(), Action.created_at.desc(), Action.id.desc()).limit(1))
    return latest if latest and latest.kind == "channel_ad_ban" else None


async def unban_payload(session, violation):
    source = await latest_ban(session, violation.group_id, violation.user_id)
    if not source or source.payload.get("violation_id") != violation.id:
        raise ManagementError(409, "该频道封禁已解除或被后续处罚替换，请核实最新违规记录")
    for pending in await session.scalars(select(Action).where(
        Action.group_id == violation.group_id, Action.user_id == violation.user_id,
        Action.kind == "channel_ad_ban", Action.state == "pending")):
        pending.state, pending.error = "cancelled", "管理员已要求解除频道封禁"
    return {"violation_id": violation.id, "channel_ban_source": source.id}


async def recheck(session, action, group, telegram):
    """Refresh trusted IDs before any channel deletion or ban, then check current evidence."""
    sender_id = action.payload["channel_evidence"].get("sender_chat_id")
    if (not group or not group.approved or not group.enabled or type(sender_id) is not int
            or sender_id >= 0 or sender_id != action.user_id or sender_id == group.id):
        raise ActionSuperseded(reason="频道身份或群状态已变化，停止旧处理", before_request=True)
    chat = await session.info["action_execution"].external(lambda: telegram.call("get_chat", chat_id=group.id))
    if not isinstance(chat, dict) or chat.get("id") != group.id:
        raise ConnectionError("暂时无法复核关联频道，稍后重试")
    await remember_linked_channel(session, group.id, chat)
    if sender_id == await own_channel_id(session, group.id):
        raise ActionSuperseded(reason="这是本群关联频道，停止处理", before_request=True)
    if action.kind == "channel_ad_ban" and GroupSettings.model_validate(group.settings).channel_ad_penalty != "ban":
        raise ActionSuperseded(reason="频道广告处理已改为只删消息，停止旧封禁", before_request=True)


class ChannelActions:
    def __init__(self, transport):
        self.transport = transport

    async def prepare(self, session, action, group, verification, member):
        if action.kind == "channel_unban":
            source = await latest_ban(session, action.group_id, action.user_id)
            if not action.actor_id or not source or source.id != action.payload.get("channel_ban_source"):
                action.state, action.error = "cancelled", "原频道封禁已解除或被替换"
                return False
            return True
        if ("channel_evidence" not in action.payload or not any(
                key in action.payload for key in ("advertisement_evidence", "message_evidence"))):
            action.state, action.error = "cancelled", "频道处罚缺少可复核的消息证据"
            return False
        return True

    async def ban(self, session, action, group, verification, member):
        await self.transport.call(session, action, "ban_chat_sender_chat",
                                  chat_id=group.id, sender_chat_id=action.user_id)

    async def unban(self, session, action, group, verification, member):
        await self.transport.call(session, action, "unban_chat_sender_chat",
                                  chat_id=group.id, sender_chat_id=action.user_id)
