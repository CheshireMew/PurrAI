"""Adapt Telegram events to application services and bound callback acknowledgements."""

from contextvars import ContextVar

from aiogram import Dispatcher
from aiogram.types import Update

from purrai.telegram import telegram_data
from purrai.auto_replies import AutoReplies
from purrai.callback_answers import CallbackAnswers
from purrai.models import Inbox

class TelegramEvents:
    def __init__(self, service, admin, chat):
        self.service, self.admin, self.chat = service, admin, chat
        self.auto_replies = AutoReplies(service.db)
        from purrai.points_bot import PointsBot
        self.answers = CallbackAnswers(service.tg, service.settings)
        self.points = PointsBot(service.db, service.tg, self.answers)
        from purrai.lottery_bot import LotteryBot
        self.lottery = LotteryBot(service.db, service.tg, self.answers)
        self.current_update_id = ContextVar("purrai_update_id", default=0)
        self.ack_tasks, self.ack_slots = self.answers.tasks, self.answers.slots
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
        if event.chat.type == "channel":
            from purrai.subscription_verification import membership_changed
            return await membership_changed(self.service.db, telegram_data(event))
        await self.service.member_event(telegram_data(event), event_id=self.current_update_id.get())

    async def bot_member(self, event):
        if event.chat.type == "channel":
            from purrai.subscription_verification import membership_changed
            return await membership_changed(self.service.db, telegram_data(event), bot_event=True)
        await self.service.member_event(telegram_data(event), True)

    async def join_request(self, event):
        await self.service.join_request(telegram_data(event), self.current_update_id.get())

    async def callback(self, event):
        query = telegram_data(event)
        if await self.lottery.callback(query):
            return
        if await self.points.callback(query):
            return
        if not await self.admin.callback(query):
            async with self.service.db.sessions() as session:
                receipt = await session.get(Inbox, self.current_update_id.get())
                received_at = receipt.received_at if receipt else None
            answer = await self.service.callback(query, acknowledge=False, received_at=received_at)
            await self.answers.send(query["id"], answer)

    async def answer_callback(self, identifier, answer):
        await self.answers.answer(identifier, answer)

    async def message(self, event):
        message = telegram_data(event)
        try:
            # Commands still pass moderation: pending members cannot evade cleanup with /help.
            if message["chat"]["type"] != "private":
                await self.service.message(
                    message, update_id=self.current_update_id.get(), archived_at_receipt=True, chat_routing=True,
                )
            if await self.admin.message(message):
                return
            if await self.lottery.observe(message):
                return
            if await self.points.observe(message):
                return
            if await self.auto_replies.observe(message):
                return
            await self.chat.observe(message, update_id=self.current_update_id.get())
        finally:
            if message["chat"]["type"] != "private":
                from purrai.advertisement_routing import finish_routing
                await finish_routing(self.service.db, message)
                self.service.advertisement_review.wakeup.set()

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
        await self.answers.close()
