"""Group conversation observation, runnable topic scheduling and worker lifecycle."""

import asyncio
import json
import logging
from contextlib import suppress

from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import delete, func, select, update

from purrai.chat_budget import BudgetCooldown, BudgetExceeded, ChatBudget
from purrai.chat_context import ChatContext
from purrai.chat_delivery import ChatDelivery
from purrai.chat_generation import ChatGeneration
from purrai.chat_model import ChatModel, ChatModelError
from purrai.chat_state import addressed as addressed
from purrai.chat_state import REPLY_DELAY, REPLY_INTERVAL, following_up, has_sent_text, human_message, session_state, store_state
from purrai.chat_state import UNREADABLE_IMAGE, has_image, ignored_text, image_message_ids, message_bursts
from purrai.diagnostics import failure
from purrai.models import ChatMessage, ChatTurn, Group, Meta, now
from purrai.schemas import GroupSettings
from purrai.stickers import Stickers, sticker_text

logger = logging.getLogger(__name__)


class GroupChat:
    def __init__(self, db, telegram, settings, model=None):
        self.db, self.tg, self.settings = db, telegram, settings
        self.model = model or ChatModel(settings)
        self.budget = ChatBudget(db, settings)
        self.busy = set()
        self.wakeup = asyncio.Event()
        self.stickers = Stickers(db, telegram)
        self.contexts = ChatContext(telegram, settings, self.stickers)
        self.delivery = ChatDelivery(db, telegram, self.contexts, self.stickers)

    async def observe(self, message, edited=False, update_id=0):
        if message.get("chat", {}).get("type") not in ("group", "supergroup"):
            return
        group_id, message_id = message["chat"]["id"], message["message_id"]
        sender = message.get("from") or {}
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
            if not row and (not cfg.chat_enabled or not self.settings.chat_ready):
                return
            image = has_image(message) and human_message({**message, "text": UNREADABLE_IMAGE})
            eligible = (
                human_message(message, cfg.chat_ignored_texts)
                and not image and not has_image(message.get("reply_to_message") or {})
            )
            if eligible or image:
                allowed = await self.contexts.allowed(session, group_id, message_id, sender["id"])
                eligible, image = eligible and allowed, image and allowed
            if image:
                text = UNREADABLE_IMAGE  # Metadata only; this row is never sent to the model.
            if row:
                if (message.get("edit_date", 0), update_id) < (row.edited_at, row.update_id):
                    return
                row.text, row.eligible = text[:1600], eligible
                row.edited_at, row.update_id = message.get("edit_date", 0), update_id
                return  # Editing and redelivery never create another reply.
            if not (eligible or image) or (edited and not image) or not cfg.chat_enabled or not self.settings.chat_ready:
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
            recent = await self.contexts.recent(session, group_id, topic_id)
            burst = next((items for items in message_bursts(recent) if row in items), [])
            if not burst:
                return  # Old recovered messages may already be outside the rolling window.
            if len(burst) > 1:
                await session.execute(update(ChatTurn).where(
                    ChatTurn.group_id == group_id,
                    ChatTurn.topic_id == topic_id,
                    ChatTurn.message_id.in_([item.message_id for item in burst]),
                    ChatTurn.state == "pending",
                ).values(available_at=func.max(ChatTurn.available_at, now() + REPLY_DELAY)))
            if image:
                return
            await self.stickers.activity(session, group_id, topic_id, cfg.chat_antifraud_interval)
            _, state = await session_state(session, group_id, topic_id)
            direct = addressed(message, self.tg, cfg.chat_name)
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
                state, sender["id"], row.received_at, allow_join=cfg.chat_proactive,
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
                    available_at=max(
                        now() + REPLY_DELAY,
                        state.get("last_reply", 0) + REPLY_INTERVAL,
                        state.get("last_attempt", 0) + REPLY_INTERVAL if followup else 0,
                    ),
                )
            )
            await session.execute(
                delete(ChatTurn).where(
                    ChatTurn.group_id == group_id,
                    ChatTurn.created_at < now() - 86400,
                    ChatTurn.state.not_in(("pending", "generating", "sending")),
                )
            )
        self.wakeup.set()

    async def recover(self):
        async with self.db.transaction() as session:
            await session.execute(
                update(ChatTurn).where(ChatTurn.state == "generating").values(state="pending")
            )
            await session.execute(
                update(ChatTurn)
                .where(ChatTurn.state == "sending")
                .values(
                    state="failed",
                    error="上次发送结果未确认，为避免重复发言未自动重发",
                )
            )

    async def claim(self):
        async with self.db.transaction() as session:
            # Select runnable conversation scopes, not a fixed page of messages:
            # one busy topic may otherwise occupy that entire page indefinitely.
            candidates = (
                await session.execute(
                    select(ChatTurn.group_id, ChatTurn.topic_id)
                    .where(
                        ChatTurn.state == "pending",
                        ChatTurn.available_at <= now(),
                    )
                    .group_by(ChatTurn.group_id, ChatTurn.topic_id)
                    .order_by(func.max(ChatTurn.direct).desc(), func.min(ChatTurn.id))
                )
            ).all()
            for first in candidates:
                key = first.group_id, first.topic_id
                if key in self.busy:
                    continue
                group = await session.get(Group, first.group_id)
                cfg = GroupSettings.model_validate(group.settings)
                batch = (
                    await session.scalars(
                        select(ChatTurn)
                        .where(
                            ChatTurn.group_id == first.group_id,
                            ChatTurn.topic_id == first.topic_id,
                            ChatTurn.state == "pending",
                            ChatTurn.available_at <= now(),
                        )
                        .order_by(ChatTurn.id)
                    )
                ).all()
                for item in batch:
                    if (
                        now() - item.created_at > 120
                        or not group.approved
                        or not cfg.chat_enabled
                        or not self.settings.chat_ready
                    ):
                        item.state = "skipped"
                        item.error = "聊天任务已过期或群聊天已停用"
                        logger.info("聊天跳过 turn=%s group=%s reason=%s", item.id, item.group_id, item.error)
                batch = [item for item in batch if item.state == "pending"]
                if not batch:
                    continue
                recent = await self.contexts.recent(session, first.group_id, first.topic_id)
                image_ids = image_message_ids(recent)
                valid_ids = {row.message_id for row in await session.scalars(select(ChatMessage).where(
                    *self.contexts.clean_conditions(first.group_id, first.topic_id),
                    ChatMessage.message_id.in_([item.message_id for item in batch]),
                )) if not ignored_text(row.text, cfg.chat_ignored_texts) and now() - row.sent_at <= 120}
                for item in batch:
                    if item.message_id in image_ids:
                        item.state, item.error = "skipped", "连续消息中含有无法读取的图片"
                    elif item.message_id not in valid_ids:
                        item.state, item.error = "skipped", "目标消息已过期、失效或被群规则排除"
                    if item.state == "skipped":
                        logger.info("聊天跳过 turn=%s group=%s reason=%s", item.id, item.group_id, item.error)
                batch = [item for item in batch if item.state == "pending"]
                if not batch:
                    continue
                state_row, state = await session_state(session, *key)
                first_direct = next((item for item in batch if item.direct), None)
                if first_direct:
                    burst_ids = next((
                        {row.message_id for row in burst}
                        for burst in message_bursts(recent)
                        if any(row.message_id == first_direct.message_id for row in burst)
                    ), {first_direct.message_id})
                    leader = next(item for item in reversed(batch) if item.direct and item.message_id in burst_ids)
                    batch = [item for item in batch if not item.direct or item.message_id in burst_ids]
                else:
                    leader = batch[-1]
                if not leader.direct and len(batch) > 1 and state.get("last_user"):
                    continuing_ids = set(await session.scalars(select(ChatMessage.message_id).where(
                        ChatMessage.group_id == first.group_id,
                        ChatMessage.topic_id == first.topic_id,
                        ChatMessage.sender_id == state["last_user"],
                        ChatMessage.message_id.in_([item.message_id for item in batch]),
                    )))
                    leader = next((item for item in reversed(batch) if item.message_id in continuing_ids), leader)
                partial = has_sent_text(leader)
                target = await session.scalar(
                    select(ChatMessage).where(
                        ChatMessage.group_id == leader.group_id,
                        ChatMessage.message_id == leader.message_id,
                        ChatMessage.eligible.is_(True),
                    )
                )
                if (
                    not target
                    or now() - target.sent_at > 120
                    or not await self.contexts.allowed(
                        session, leader.group_id, leader.message_id, target.sender_id
                    )
                ):
                    for item in batch:
                        item.state = "skipped"
                        item.error = "目标消息已过期、失效或被群规则排除"
                    logger.info("聊天跳过 turn=%s group=%s reason=%s", leader.id, leader.group_id, leader.error)
                    continue
                if (
                    now() < state.get("blocked_until", 0)
                    or not partial
                    and now() < state.get("last_reply", 0) + REPLY_INTERVAL
                ):
                    for item in batch:
                        item.available_at = max(
                            state.get("blocked_until", 0), state.get("last_reply", 0) + REPLY_INTERVAL
                        )
                    continue
                target_burst = next((
                    {row.message_id for row in burst}
                    for burst in message_bursts(recent)
                    if any(row.message_id == target.message_id for row in burst)
                ), {target.message_id})
                followup = following_up(
                    state, target.sender_id, target.received_at, allow_join=cfg.chat_proactive,
                    message_id=target.message_id, burst_ids=target_burst,
                )
                audience = "none"
                if target.reply_to_id:
                    source = await session.scalar(
                        select(ChatMessage).where(
                            ChatMessage.group_id == target.group_id,
                            ChatMessage.message_id == target.reply_to_id,
                        )
                    )
                    bot_reply = bool(source and source.is_bot and source.sender_id == self.tg.user_id)
                    audience = "bot" if bot_reply else "other"
                    followup = followup and bot_reply
                if not leader.direct and not partial and not (cfg.chat_proactive or followup):
                    for item in batch:
                        item.state = "skipped"
                        item.error = "主动聊天关闭，且不是直接对话或续聊"
                    logger.info("聊天跳过 turn=%s group=%s reason=%s", leader.id, leader.group_id, leader.error)
                    continue
                mode = "direct" if leader.direct else "followup" if followup else "ambient"
                if not leader.direct and "广告" in target.text and not (
                    mode == "followup" and state.get("ad_conversation") and state.get("last_user") == target.sender_id
                ):
                    for item in batch:
                        item.state, item.error = "skipped", "普通广告议论不主动插话"
                        logger.info("聊天跳过 turn=%s group=%s reason=%s", item.id, item.group_id, item.error)
                    continue
                if mode == "followup" and not leader.attempts and now() < state.get("last_attempt", 0) + REPLY_INTERVAL:
                    for item in batch:
                        item.available_at = state["last_attempt"] + REPLY_INTERVAL
                    continue
                if mode == "ambient" and not partial:
                    attention = await session.get(Meta, f"chat_attention:{leader.group_id}")
                    activity = json.loads(attention.value) if attention else {}
                    if (
                        now()
                        < max(activity.get("last_attempt", 0), activity.get("last_reply", 0))
                        + cfg.chat_interval
                    ):
                        for item in batch:
                            item.state = "skipped"
                            item.error = "主动插话冷却尚未结束"
                        logger.info("聊天跳过 turn=%s group=%s reason=%s", leader.id, leader.group_id, leader.error)
                        continue
                    activity["last_attempt"] = now()
                    encoded = json.dumps(activity)
                    if attention:
                        attention.value = encoded
                    else:
                        session.add(Meta(key=f"chat_attention:{leader.group_id}", value=encoded))
                for item in batch:
                    item.state = "generating" if item.id == leader.id else "merged"
                context = await self.contexts.build(session, leader, cfg)
                if not any(row["id"] == leader.message_id for row in context):
                    for item in batch:
                        item.state = "skipped"
                        item.error = "目标消息未进入有效上下文"
                    logger.info("聊天跳过 turn=%s group=%s reason=%s", leader.id, leader.group_id, leader.error)
                    continue
                leader.attempts += 1
                state["last_attempt"] = now()
                store_state(session, state_row, *key, state)
                self.busy.add(key)
                return leader, cfg, context, target.text, target.sender_id, mode, audience

    async def run_one(self):
        claimed = await self.claim()
        if not claimed:
            return False
        turn, cfg, context, original, sender_id, mode, audience = claimed
        key = turn.group_id, turn.topic_id
        try:
            partial = has_sent_text(turn)
            if mode in ("direct", "followup") and not turn.response:
                with suppress(Exception):
                    await self.tg.call(
                        "send_chat_action",
                        chat_id=turn.group_id,
                        action="typing",
                        **({"message_thread_id": turn.topic_id} if turn.topic_id else {}),
                        request_timeout=2,
                    )
            # Typing notification can yield to moderation. Refresh just before the paid request.
            async with self.db.transaction() as session:
                current = await session.get(ChatTurn, turn.id)
                group = await session.get(Group, turn.group_id)
                cfg = GroupSettings.model_validate(group.settings)
                context = await self.contexts.build(session, turn, cfg)
                if (
                    current.state != "generating"
                    or not group.approved
                    or not cfg.chat_enabled
                    or not any(row["id"] == turn.message_id for row in context)
                ):
                    current.state = "skipped"
                    current.error = "请求模型前，聊天任务、群设置或目标消息已失效"
                    logger.info("聊天跳过 turn=%s group=%s reason=%s", turn.id, turn.group_id, current.error)
                    return True
                if not partial:
                    await self.contexts.remember_sent_context(session, turn, context)
                sticker_options = await self.stickers.options(session, turn.group_id, cfg)
            generated = await ChatGeneration(self.model, self.budget).generate(
                turn, cfg, context, sender_id, mode, audience, sticker_options
            )
            return await self.delivery.deliver(turn, cfg, original, sender_id, generated)
        except BudgetCooldown as exc:
            async with self.db.transaction() as session:
                current = await session.get(ChatTurn, turn.id)
                if current.state == "generating":
                    current.state, current.available_at, current.error = "pending", exc.retry_at, str(exc)
                    current.attempts -= 1  # No model request was made.
                    logger.info(
                        "聊天等待 turn=%s group=%s retry_at=%.3f reason=%s",
                        turn.id, turn.group_id, exc.retry_at, current.error,
                    )
        except BudgetExceeded as exc:
            async with self.db.transaction() as session:
                current = await session.get(ChatTurn, turn.id)
                current.state, current.error = "skipped", str(exc)
                logger.info("聊天未请求模型 turn=%s group=%s reason=%s", turn.id, turn.group_id, current.error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.db.transaction() as session:
                current = await session.get(ChatTurn, turn.id)
                retry = (
                    isinstance(exc, ChatModelError)
                    and exc.retryable
                    and turn.attempts < 2
                    and current.state == "generating"
                )
                telegram_retry = isinstance(exc, TelegramRetryAfter) and turn.attempts < 3
                retry = retry or telegram_retry
                current.state = "pending" if retry else "failed"
                current.available_at = now() + (exc.retry_after if telegram_retry else 5)
                current.error = failure(
                    logger,
                    self.settings,
                    exc,
                    "聊天回复重试" if retry else "聊天回复失败",
                    f"turn={turn.id} group={turn.group_id} topic={turn.topic_id} message={turn.message_id}",
                    terminal=not retry,
                )
                if not retry:
                    state_row, state = await session_state(session, *key)
                    state["blocked_until"] = now() + 60
                    store_state(session, state_row, *key, state)
        finally:
            self.busy.discard(key)
        return True

    async def loop(self):
        while True:
            try:
                if not self.tg.connected or not self.tg.user_id:
                    await asyncio.sleep(0.5)
                    continue
                self.wakeup.clear()
                if not await self.run_one():
                    try:
                        await asyncio.wait_for(self.wakeup.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.settings, exc, "聊天循环异常", terminal=True)
                await asyncio.sleep(2)

    async def run(self):
        await self.recover()
        await asyncio.gather(self.loop(), self.loop(), self.sticker_loop())

    async def sticker_loop(self):
        while True:
            try:
                if self.tg.connected and self.tg.user_id:
                    await self.stickers.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.settings, exc, "贴纸列表或防诈提醒失败")
            await asyncio.sleep(60)

    async def status(self, group_id):
        async with self.db.sessions() as session:
            latest = await session.scalar(
                select(ChatTurn)
                .where(
                    ChatTurn.group_id == group_id,
                    ChatTurn.error != "",
                    ChatTurn.created_at >= now() - 86400,
                )
                .order_by(ChatTurn.id.desc())
                .limit(1)
            )
            pending = await session.scalar(
                select(func.count())
                .select_from(ChatTurn)
                .where(
                    ChatTurn.group_id == group_id,
                    ChatTurn.state.in_(("pending", "generating", "sending")),
                )
            )
            group = await session.get(Group, group_id)
            cfg = GroupSettings.model_validate(group.settings)
        return {
            "configured": self.settings.chat_ready,
            "model": self.settings.chat_model,
            "pending": pending,
            "error": latest.error if latest else "",
            "budget": await self.budget.status(group_id),
            "stickers": await self.stickers.status(cfg, group_id),
        }

    async def close(self):
        await self.model.close()
