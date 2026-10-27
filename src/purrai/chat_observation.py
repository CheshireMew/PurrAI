"""Record incoming chat messages and decide whether to queue a reply."""

import logging

from sqlalchemy import delete, select

from purrai.chat_input import input_reasons
from purrai import chat_commands
from purrai.chat_retraction import RECEIPT_RETENTION
from purrai.group_link_identity import own_channel_id
from purrai.chat_state import (
    UNREADABLE_IMAGE, addressed, chat_sender, following_up, has_image, human_message, ignored_text,
    message_bursts, reply_snapshot, session_state,
)
from purrai.models import ChatMessage, ChatTurn, Group, Rule, now
from purrai.rules import message_content
from purrai.schemas import GroupSettings
from purrai.serialization import row_dict
from purrai.stickers import sticker_text
from purrai.telegram_sources import channel_sources, external_channel_reasons

logger = logging.getLogger(__name__)


class ChatObservation:
    def __init__(self, db, telegram, settings, contexts, stickers, wakeup):
        self.db, self.tg, self.settings = db, telegram, settings
        self.contexts, self.stickers, self.wakeup = contexts, stickers, wakeup

    async def observe(self, message, edited=False, update_id=0):
        if message.get("chat", {}).get("type") not in ("group", "supergroup"):
            return
        group_id, message_id = message["chat"]["id"], message["message_id"]
        sender = chat_sender(message)
        text = sticker_text(message)
        async with self.db.transaction() as session:
            row = await session.scalar(
                select(ChatMessage).where(
                    ChatMessage.group_id == group_id,
                    ChatMessage.message_id == message_id,
                )
            )
            group = await session.get(Group, group_id)
            if not group or not group.approved:
                return
            cfg = GroupSettings.model_validate(group.settings)
            if row and (message.get("edit_date", 0), update_id) < (row.edited_at, row.update_id):
                return
            if not row and (not cfg.chat_enabled or not self.settings.chat_ready):
                return
            direct = addressed(message, self.tg, cfg.chat_name)
            rules = [row_dict(rule) for rule in await session.scalars(select(Rule).where(Rule.group_id == group_id))]
            urls = message_content(message)[1]
            reasons = input_reasons(text, rules, group.settings, direct=direct, urls=urls)
            reasons += external_channel_reasons(channel_sources(message), group_id, group.settings, rules,
                                                linked_channel_id=await own_channel_id(session, group_id))
            allowed = bool(sender.get("id")) and await self.contexts.allowed(session, group_id, message_id, sender["id"])
            command_state = await chat_commands.observe(
                session, group_id, {**message, "from": sender}, cfg,
                eligible=bool(not direct and not reasons and not urls and not has_image(message)
                              and not (message.get("forward_origin") or message.get("forward_date"))
                              and human_message(message, bot_username=getattr(self.tg, "username", "")) and allowed),
                edited=edited, advertising=bool(reasons),
            )
            ignored = cfg.chat_ignored_texts + chat_commands.learned(command_state, cfg)
            command = ignored_text(text, ignored, getattr(self.tg, "username", ""))
            if chat_commands.known_ad(command_state, text):
                reasons.append("聊天广告历史命中")
            image = has_image(message) and human_message({**message, "text": UNREADABLE_IMAGE})
            eligible = human_message(
                {**message, "text": text or UNREADABLE_IMAGE} if image else message, ignored,
                bot_username=getattr(self.tg, "username", ""),
            ) and (direct or not (image or has_image(message.get("reply_to_message") or {})))
            if command:
                image, eligible = False, False
                logger.info("聊天口令已排除 group=%s message=%s", group_id, message_id)
            eligible = eligible and not reasons
            if eligible or image:
                eligible, image = eligible and allowed, image and allowed
            if image and not reasons:
                text = UNREADABLE_IMAGE + ("\n" + text if text else "")
            if reasons:
                image = False
                logger.info("聊天输入已拦截 group=%s message=%s reasons=%s", group_id, message_id, reasons)
            if row:
                row.text, row.eligible = text[:1600], eligible
                row.reply_text = reply_snapshot(message, direct=direct)
                row.edited_at, row.update_id = message.get("edit_date", 0), update_id
                return  # Editing and redelivery never create another reply.
            if not (eligible or image or reasons and human_message(message)) or (edited and not image) or not cfg.chat_enabled or not self.settings.chat_ready:
                return
            # Ordinary supergroup replies also carry a thread ID. Only forum
            # topic messages belong to separate conversations and send targets.
            topic_id = (message.get("message_thread_id") or 0) if message.get("is_topic_message") else 0
            reply = message.get("reply_to_message") or {}
            row = ChatMessage(
                group_id=group_id,
                topic_id=topic_id,
                message_id=message_id,
                sender_id=sender["id"],
                sender_name=" ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))[
                    :100
                ],
                text=text[:1600],
                reply_to_id=reply.get("message_id", 0),
                reply_text=reply_snapshot(message, direct=direct),
                sent_at=message["date"],
                received_at=now(),
                update_id=update_id,
                eligible=eligible,
            )
            session.add(row)
            await session.flush()
            # Only a small rolling chat window is kept; the independent archive is untouched.
            older = (
                select(ChatMessage.id)
                .where(
                    ChatMessage.group_id == group_id,
                    ChatMessage.topic_id == topic_id,
                )
                .order_by(ChatMessage.sent_at.desc(), ChatMessage.message_id.desc())
                .offset(80)
            )
            await session.execute(delete(ChatMessage).where(ChatMessage.id.in_(older)))
            if not await self.schedule(session, row, cfg, message, direct, image, reasons):
                return
        self.wakeup.set()

    async def schedule(self, session, row, cfg, message, direct, image, reasons):
        group_id, topic_id = row.group_id, row.topic_id
        message_id = row.message_id
        text, reply = row.text, message.get("reply_to_message") or {}
        recent = await self.contexts.recent(session, group_id, topic_id)
        burst = next((items for items in message_bursts(recent) if row in items), [])
        if not burst:
            return  # Old recovered messages may already be outside the rolling window.
        if reasons or image and not direct:
            return
        await self.stickers.activity(session, group_id, topic_id, cfg.chat_antifraud_interval)
        _, state = await session_state(session, group_id, topic_id)
        reply_elsewhere = bool(reply and reply.get("from", {}).get("id") != self.tg.user_id)
        inflight = False
        if not direct and not reply_elsewhere:
            # A supplement can arrive while the answer to this same burst
            # is still being generated, before last_reply has been recorded.
            inflight = bool(await session.scalar(select(ChatTurn.id).where(
                ChatTurn.group_id == group_id, ChatTurn.topic_id == topic_id,
                ChatTurn.message_id.in_([item.message_id for item in burst]),
                ChatTurn.state == "generating",
            )))
        followup = not reply_elsewhere and (inflight or following_up(
            state, row.sender_id, row.received_at, allow_join=cfg.chat_proactive,
        ))
        if not direct and not followup:
            reason = (
                "主动聊天关闭，且不是直接对话或续聊" if not cfg.chat_proactive
                else "普通插话内容过短" if len(text) < 4
                else "聊天失败后的暂停尚未结束" if now() < state.get("blocked_until", 0)
                else "主动插话冷却尚未结束"
                if now() - state.get("last_attempt", 0) < cfg.chat_interval else ""
            )
            if reason:
                logger.info(
                    "聊天未进入模型 group=%s topic=%s message=%s reason=%s",
                    group_id, topic_id, message_id, reason,
                )
                return
        if now() - message["date"] > 120:
            logger.info(
                "聊天未进入模型 group=%s topic=%s message=%s reason=消息已过期",
                group_id, topic_id, message_id,
            )
            return  # Recovered history supplies context, never belated interjections.
        session.add(
            ChatTurn(
                group_id=group_id,
                topic_id=topic_id,
                message_id=message_id,
                direct=direct,
                available_at=now(),
            )
        )
        await session.execute(
            delete(ChatTurn).where(
                ChatTurn.group_id == group_id,
                ChatTurn.created_at < now() - RECEIPT_RETENTION,
                ChatTurn.state.not_in(("pending", "generating", "sending")),
            )
        )
        return True
