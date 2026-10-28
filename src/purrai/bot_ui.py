"""Telegram transport and reusable panel rendering."""

import asyncio
import hashlib
import json
import logging
import secrets

from aiogram.exceptions import TelegramBadRequest

from purrai.bot_common import keyboard_rows
from purrai.diagnostics import failure

logger = logging.getLogger(__name__)


class BotUI:
    def __init__(self, telegram, settings, state):
        self.tg, self.settings, self.state = telegram, settings, state
        self.ack_tasks = set()
        self.ack_slots = asyncio.Semaphore(8)
        self.closed = False

    async def acknowledge(self, identifier, answer):
        if self.closed:
            return
        if len(self.ack_tasks) >= 64:
            logger.warning("管理提示回执队列已满 callback=%s", identifier)
            return
        task = asyncio.create_task(self.answer(identifier, answer), name="admin-callback-answer")
        self.ack_tasks.add(task)
        task.add_done_callback(self.ack_tasks.discard)
        # Start the acknowledgement promptly without waiting for its network reply.
        await asyncio.sleep(0)

    async def answer(self, identifier, answer):
        try:
            async with asyncio.timeout(4):
                async with self.ack_slots:
                    await self.tg.call(
                        "answer_callback_query", callback_query_id=identifier, text=answer, request_timeout=3
                    )
        except Exception as exc:
            failure(logger, self.settings, exc, "管理提示回执失败", f"callback={identifier}")

    async def close(self):
        self.closed = True
        tasks = list(self.ack_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def send(self, chat_id, text):
        return await self.tg.call("send_message", chat_id=chat_id, text=text[:4000])

    async def panel(self, actor, text, buttons, group_id=None, message_id=None, *, resume=None):
        resume = resume or (
            {"action": "group", "data": {"id": group_id}}
            if group_id is not None
            else {"action": "groups", "data": {}}
        )
        fingerprint = hashlib.sha256(json.dumps(
            [group_id, buttons, resume], sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()
        text_hash = hashlib.sha256(text[:4000].encode()).hexdigest()
        current = await self.state.recall(f"bot_panel:{actor}:{message_id}") if message_id else None
        same_controls = current and current.get("fingerprint") == fingerprint
        panel_id = current["panel"] if same_controls else secrets.token_hex(8)
        # Text-only updates and stale-click recovery must not invalidate a fresh button.
        keyboard = await self.state.buttons(
            actor, buttons, group_id, resume, panel_id=panel_id,
            previous=current.get("keyboard") if same_controls else None,
        )
        if same_controls and current.get("text_hash") == text_hash and keyboard == current.get("keyboard"):
            return
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
                await self.state.publish_panel(actor, message_id, panel_id, group_id, resume,
                                               fingerprint, text_hash, keyboard)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
        result = await self.tg.call("send_message", chat_id=actor, text=text[:4000], reply_markup=markup)
        await self.state.publish_panel(actor, result["message_id"], panel_id, group_id, resume,
                                       fingerprint, text_hash, keyboard)
        if message_id:
            await self.state.publish_panel(actor, message_id, panel_id, group_id, resume,
                                           fingerprint, text_hash, keyboard)
