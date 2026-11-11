"""Adapt Telegram events to application services and bound callback acknowledgements."""

import asyncio
import logging
from contextvars import ContextVar

from aiogram import Dispatcher
from aiogram.types import Update

from purrai.diagnostics import failure
from purrai.telegram import telegram_data
from purrai.auto_replies import AutoReplies

logger = logging.getLogger(__name__)


class TelegramEvents:
    def __init__(self, service, admin, chat):
        self.service, self.admin, self.chat = service, admin, chat
        self.auto_replies = AutoReplies(service.db)
        self.current_update_id = ContextVar("purrai_update_id", default=0)
        self.ack_tasks, self.ack_slots = set(), asyncio.Semaphore(8)
        self.dispatcher = Dispatcher()
        self.dispatcher.chat_member.register(self.member)
        self.dispatcher.my_chat_member.register(self.bot_member)
        self.dispatcher.chat_join_request.register(self.join_request)
        self.dispatcher.callback_query.register(self.callback)
        self.dispatcher.message.register(self.message)
        self.dispatcher.edited_message.register(self.edited)

    async def dispatch(self, payload):
        # Bind during validation so Dispatcher need not round-trip SDK defaults.
        bot = self.service.tg.bot
        update = Update.model_validate(payload, context={"bot": bot})
        await self.dispatcher.feed_update(bot, update)

    async def member(self, event):
        await self.service.member_event(telegram_data(event), event_id=self.current_update_id.get())

    async def bot_member(self, event):
        await self.service.member_event(telegram_data(event), True)

    async def join_request(self, event):
        await self.service.join_request(telegram_data(event), self.current_update_id.get())

    async def callback(self, event):
        query = telegram_data(event)
        if not await self.admin.callback(query):
            answer = await self.service.callback(query, acknowledge=False)
            # Input is committed before acknowledgement, but slow toasts never
            # hold up the next digit. Bound both the task count and HTTP lifetime.
            if len(self.ack_tasks) < 64:
                task = asyncio.create_task(self.answer_callback(query["id"], answer))
                self.ack_tasks.add(task)
                task.add_done_callback(self.ack_tasks.discard)

    async def answer_callback(self, identifier, answer):
        try:
            async with asyncio.timeout(4):
                async with self.ack_slots:
                    await self.service.acknowledge(identifier, answer)
        except TimeoutError as exc:
            failure(logger, self.service.settings, exc, "验证提示回执超时（输入已保存）", f"callback={identifier}")

    async def message(self, event):
        message = telegram_data(event)
        # Commands still pass moderation: pending members cannot evade cleanup with /help.
        if message["chat"]["type"] != "private":
            await self.service.message(
                message, update_id=self.current_update_id.get(), archived_at_receipt=True
            )
        await self.admin.message(message)
        if await self.auto_replies.observe(message):
            return
        await self.chat.observe(message, update_id=self.current_update_id.get())

    async def edited(self, event):
        message = telegram_data(event)
        await self.service.message(
            message,
            True,
            update_id=self.current_update_id.get(),
            archived_at_receipt=True,
        )
        await self.auto_replies.observe(message, edited=True)
        await self.chat.observe(message, edited=True, update_id=self.current_update_id.get())

    async def close(self):
        tasks = list(self.ack_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
