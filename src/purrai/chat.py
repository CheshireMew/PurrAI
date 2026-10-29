"""Group conversation observation, runnable topic scheduling and worker lifecycle."""

import asyncio
import logging
from contextlib import suppress

from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import func, select, update

from purrai.shared_policy import effective_rules
from purrai.chat_budget import BudgetCooldown, BudgetExceeded, ChatBudget
from purrai import chat_commands
from purrai.chat_context import ChatContext
from purrai.chat_delivery import ChatDelivery
from purrai.chat_generation import ChatGeneration
from purrai.chat_observation import ChatObservation
from purrai.chat_receipts import ChatReceipts
from purrai.chat_scheduler import ChatScheduler
from purrai.chat_model import ChatModel, ChatModelError
from purrai.chat_retraction import recover_retractions
from purrai.chat_state import addressed as addressed
from purrai.chat_state import has_sent_text, session_state, store_state
from purrai.diagnostics import failure
from purrai.models import ChatTurn, Group, now
from purrai.schemas import GroupSettings
from purrai.stickers import Stickers

logger = logging.getLogger(__name__)


class GroupChat:
    def __init__(self, db, telegram, settings, model=None):
        self.db, self.tg, self.settings = db, telegram, settings
        self.model = model or ChatModel(settings)
        self.budget = ChatBudget(db, settings)
        self.busy = set()
        self.wakeup = asyncio.Event()
        self.stickers = Stickers(db, telegram)
        self.contexts = ChatContext(telegram)
        self.receipts = ChatReceipts(telegram, settings, self.stickers)
        self.observation = ChatObservation(db, telegram, settings, self.contexts, self.stickers, self.wakeup)
        self.scheduler = ChatScheduler(db, telegram, settings, self.contexts, self.busy)
        self.delivery = ChatDelivery(db, telegram, self.contexts, self.stickers, self.receipts)

    async def observe(self, message, edited=False, update_id=0):
        return await self.observation.observe(message, edited, update_id)

    async def recover(self):
        await recover_retractions(self.db)
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
        return await self.scheduler.claim()

    async def run_one(self):
        claimed = await self.claim()
        if not claimed:
            return False
        turn, cfg, context, original, sender_id = claimed
        key = turn.group_id, turn.topic_id
        try:
            partial = has_sent_text(turn)
            if turn.direct and not turn.response:
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
            generated = await ChatGeneration(self.model, self.budget, self.tg).generate(
                turn, cfg, context, sender_id, sticker_options
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
                if current.state in ("retracting", "retracted", "retraction_failed"):
                    return True
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
        # The owner must await every child's cancellation cleanup before the
        # application closes SQLite, including workers still leaving a session.
        async with asyncio.TaskGroup() as workers:
            workers.create_task(self.loop())
            workers.create_task(self.loop())
            workers.create_task(self.sticker_loop())

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
            _, command_state = await chat_commands.load(session, group_id)
            rules = await effective_rules(session, group_id)
        return {
            "configured": self.settings.chat_ready,
            "model": self.settings.chat_model,
            "pending": pending,
            "error": latest.error if latest else "",
            "budget": await self.budget.status(group_id),
            "stickers": await self.stickers.status(cfg, group_id),
            "commands": chat_commands.status(command_state, cfg, rules),
        }

    async def close(self):
        await self.model.close()
