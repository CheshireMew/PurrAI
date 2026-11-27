"""Telegram transport and reusable panel rendering."""

import hashlib
import json
import secrets

from aiogram.exceptions import TelegramBadRequest

from purrai.bot_common import keyboard_rows
from purrai.callback_answers import CallbackAnswers

class BotUI:
    def __init__(self, telegram, settings, state):
        self.tg, self.settings, self.state = telegram, settings, state
        self.answers = CallbackAnswers(telegram, settings)
        self.ack_tasks, self.ack_slots = self.answers.tasks, self.answers.slots

    async def acknowledge(self, identifier, answer):
        await self.answers.send(identifier, answer)

    async def answer(self, identifier, answer):
        await self.answers.answer(identifier, answer)

    async def close(self):
        await self.answers.close()

    async def send(self, chat_id, text, *, request_id=None):
        if chat_id < 0:
            from purrai.action_queue import enqueue
            from purrai.models import uid
            async with self.state.db.transaction() as session:
                key = f"bot-reply:{request_id}:{hashlib.sha256(text.encode()).hexdigest()}" if request_id else f"bot-reply:{uid()}"
                return await enqueue(session, "admin_notify", key, chat_id,
                    payload={"chat_id": chat_id, "text": text[:4000]}, priority=20)
        return await self.tg.call("send_message", chat_id=chat_id, text=text[:4000])

    async def cleanup_command(self, message):
        from purrai.action_queue import enqueue
        from purrai.models import Group, now
        from purrai.schemas import GroupSettings
        chat_id = message["chat"]["id"]
        if chat_id >= 0:
            return
        async with self.state.db.transaction() as session:
            group = await session.get(Group, chat_id)
            if not group or not group.approved:
                return
            action = await enqueue(session, "delete", f"cleanup:command:{chat_id}:{message['message_id']}", chat_id,
                payload={"chat_id": chat_id, "message_id": message["message_id"]}, priority=5,
                reason="群内管理命令自动清理")
            if not action.payload.get("expires_at"):
                deadline = now() + GroupSettings.model_validate(group.settings).notice_seconds
                action.payload = {**action.payload, "expires_at": deadline}
                action.available_at = deadline

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
