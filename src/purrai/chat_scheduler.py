"""Select runnable conversations, merge pending turns and reserve one reply."""

import json
import logging

from sqlalchemy import func, select

from purrai import chat_commands
from purrai.chat_state import (
    following_up, has_sent_text, ignored_text, image_message_ids,
    message_bursts, session_state, store_state,
)
from purrai.models import ChatMessage, ChatTurn, Group, Meta, now
from purrai.schemas import GroupSettings

logger = logging.getLogger(__name__)


class ChatScheduler:
    def __init__(self, db, telegram, settings, contexts, busy):
        self.db, self.tg, self.settings = db, telegram, settings
        self.contexts, self.busy = contexts, busy

    @staticmethod
    def skip(batch, reason):
        for item in batch:
            item.state, item.error = "skipped", reason
            logger.info("聊天跳过 turn=%s group=%s reason=%s", item.id, item.group_id, reason)

    async def claim(self):
        async with self.db.transaction() as session:
            # Enumerate scopes so a busy topic cannot hide unrelated conversations.
            candidates = (await session.execute(
                select(ChatTurn.group_id, ChatTurn.topic_id)
                .where(ChatTurn.state == "pending", ChatTurn.available_at <= now())
                .group_by(ChatTurn.group_id, ChatTurn.topic_id)
                .order_by(func.max(ChatTurn.direct).desc(), func.min(ChatTurn.id))
            )).all()
            for scope in candidates:
                key = scope.group_id, scope.topic_id
                if key not in self.busy:
                    prepared = await self.prepare(session, key)
                    if prepared:
                        return prepared

    async def pending_batch(self, session, group, topic_id, cfg):
        _, command_state = await chat_commands.load(session, group.id)
        ignored = cfg.chat_ignored_texts + chat_commands.learned(command_state, cfg)
        batch = (await session.scalars(select(ChatTurn).where(
            ChatTurn.group_id == group.id, ChatTurn.topic_id == topic_id,
            ChatTurn.state == "pending", ChatTurn.available_at <= now(),
        ).order_by(ChatTurn.id))).all()
        for item in batch:
            if (now() - item.created_at > 120 or not group.approved
                    or not cfg.chat_enabled or not self.settings.chat_ready):
                self.skip([item], "聊天任务已过期或群聊天已停用")
        batch = [item for item in batch if item.state == "pending"]
        if not batch:
            return [], []
        recent = await self.contexts.recent(session, group.id, topic_id)
        image_ids = image_message_ids(recent)
        valid_ids = {row.message_id for row in await session.scalars(select(ChatMessage).where(
            *self.contexts.clean_conditions(group.id, topic_id),
            ChatMessage.message_id.in_([item.message_id for item in batch]),
        )) if not ignored_text(row.text, ignored, getattr(self.tg, "username", "")) and now() - row.sent_at <= 120}
        for item in batch:
            if item.message_id in image_ids and not item.direct:
                self.skip([item], "连续消息中含有无法读取的图片")
            elif item.message_id not in valid_ids:
                self.skip([item], "目标消息已过期、失效或被群规则排除")
        return [item for item in batch if item.state == "pending"], recent

    @staticmethod
    def burst_ids(recent, message_id):
        return next((
            {row.message_id for row in burst}
            for burst in message_bursts(recent)
            if any(row.message_id == message_id for row in burst)
        ), {message_id})

    async def choose_target(self, session, batch, recent, state):
        first_direct = next((item for item in batch if item.direct), None)
        if first_direct:
            burst = self.burst_ids(recent, first_direct.message_id)
            leader = next(item for item in reversed(batch) if item.direct and item.message_id in burst)
            batch = [item for item in batch if not item.direct or item.message_id in burst]
        else:
            leader = batch[-1]
        if not leader.direct and len(batch) > 1 and state.get("last_user"):
            continuing = set(await session.scalars(select(ChatMessage.message_id).where(
                ChatMessage.group_id == leader.group_id, ChatMessage.topic_id == leader.topic_id,
                ChatMessage.sender_id == state["last_user"],
                ChatMessage.message_id.in_([item.message_id for item in batch]),
            )))
            leader = next((item for item in reversed(batch) if item.message_id in continuing), leader)
        target = await session.scalar(select(ChatMessage).where(
            ChatMessage.group_id == leader.group_id, ChatMessage.message_id == leader.message_id,
            ChatMessage.eligible.is_(True),
        ))
        if (not target or now() - target.sent_at > 120
                or not await self.contexts.allowed(session, leader.group_id, leader.message_id, target.sender_id)):
            self.skip(batch, "目标消息已过期、失效或被群规则排除")
            return None
        return batch, leader, target

    async def reply_mode(self, session, batch, leader, target, recent, state, cfg):
        partial = has_sent_text(leader)
        followup = following_up(
            state, target.sender_id, target.received_at, allow_join=cfg.chat_proactive,
            message_id=target.message_id, burst_ids=self.burst_ids(recent, target.message_id),
        )
        audience = "none"
        if target.reply_to_id:
            source = await session.scalar(select(ChatMessage).where(
                ChatMessage.group_id == target.group_id, ChatMessage.message_id == target.reply_to_id,
            ))
            bot_reply = bool(source and source.is_bot and source.sender_id == self.tg.user_id)
            audience = "bot" if bot_reply else "other"
            followup = followup and bot_reply
        if not leader.direct and not partial and not (cfg.chat_proactive or followup):
            self.skip(batch, "主动聊天关闭，且不是直接对话或续聊")
            return None
        mode = "direct" if leader.direct else "followup" if followup else "ambient"
        if not leader.direct and "广告" in target.text and not (
            mode == "followup" and state.get("ad_conversation") and state.get("last_user") == target.sender_id
        ):
            self.skip(batch, "普通广告议论不主动插话")
            return None
        if mode == "ambient" and not partial:
            attention = await session.get(Meta, f"chat_attention:{leader.group_id}")
            activity = json.loads(attention.value) if attention else {}
            if now() < max(activity.get("last_attempt", 0), activity.get("last_reply", 0)) + cfg.chat_interval:
                self.skip(batch, "主动插话冷却尚未结束")
                return None
            activity["last_attempt"] = now()
            encoded = json.dumps(activity)
            if attention:
                attention.value = encoded
            else:
                session.add(Meta(key=f"chat_attention:{leader.group_id}", value=encoded))
        return mode, audience

    async def prepare(self, session, key):
        group = await session.get(Group, key[0])
        cfg = GroupSettings.model_validate(group.settings)
        batch, recent = await self.pending_batch(session, group, key[1], cfg)
        if not batch:
            return None
        state_row, state = await session_state(session, *key)
        chosen = await self.choose_target(session, batch, recent, state)
        if not chosen:
            return None
        batch, leader, target = chosen
        if now() < state.get("blocked_until", 0):
            for item in batch:
                item.available_at = state["blocked_until"]
            return None
        mode = await self.reply_mode(session, batch, leader, target, recent, state, cfg)
        if mode is None:
            return None
        for item in batch:
            item.state = "generating" if item.id == leader.id else "merged"
        context = await self.contexts.build(session, leader, cfg)
        if not any(row["id"] == leader.message_id for row in context):
            self.skip(batch, "目标消息未进入有效上下文")
            return None
        leader.attempts += 1
        state["last_attempt"] = now()
        store_state(session, state_row, *key, state)
        self.busy.add(key)
        logger.info("聊天选择 turn=%s group=%s topic=%s message=%s mode=%s audience=%s",
                    leader.id, leader.group_id, leader.topic_id, leader.message_id, *mode)
        return leader, cfg, context, target.text, target.sender_id, *mode
