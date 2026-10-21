"""Telegram transport and reusable panel rendering."""

import logging

from aiogram.exceptions import TelegramBadRequest

from purrai.bot_common import keyboard_rows
from purrai.diagnostics import failure

logger = logging.getLogger(__name__)


class BotUI:
    def __init__(self, telegram, settings, state):
        self.tg, self.settings, self.state = telegram, settings, state

    async def acknowledge(self, identifier, answer):
        try:
            await self.tg.call(
                "answer_callback_query", callback_query_id=identifier, text=answer, request_timeout=3
            )
        except Exception as exc:
            failure(logger, self.settings, exc, "管理提示回执失败", f"callback={identifier}")

    async def send(self, chat_id, text):
        return await self.tg.call("send_message", chat_id=chat_id, text=text[:4000])

    async def panel(self, actor, text, buttons, group_id=None, message_id=None, *, resume=None):
        resume = resume or (
            {"action": "group", "data": {"id": group_id}}
            if group_id is not None
            else {"action": "groups", "data": {}}
        )
        keyboard = await self.state.buttons(actor, buttons, group_id, resume)
        markup = {"inline_keyboard": keyboard_rows(keyboard)}
        if message_id:
            try:
                await self.tg.call(
                    "edit_message_text",
                    chat_id=actor,
                    message_id=message_id,
                    text=text[:4000],
                    reply_markup=markup,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
        await self.tg.call("send_message", chat_id=actor, text=text[:4000], reply_markup=markup)
