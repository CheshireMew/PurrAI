"""Persist confirmed chat output, conversation activity and its source receipts."""

import json

from purrai.archive import capture
from purrai.chat_retraction import remember_receipt
from purrai.chat_state import session_state, store_state
from purrai.models import ChatMessage, ChatTurn, Meta, now
from purrai.stickers import sticker_text
from purrai.telegram import bot_display_name


class ChatReceipts:
    def __init__(self, telegram, settings, stickers):
        self.tg, self.settings, self.stickers = telegram, settings, stickers

    async def record(self, session, turn, cfg, group, sent, sender_id, *, text=None, sticker=None):
        current = await session.get(ChatTurn, turn.id)
        current.sent_message_id = sent["message_id"]
        name = bot_display_name(self.tg, cfg.chat_name)
        session.add(
            ChatMessage(
                group_id=turn.group_id,
                topic_id=turn.topic_id,
                message_id=sent["message_id"],
                sender_id=self.tg.user_id,
                sender_name=name,
                text=sticker_text({"sticker": sticker}) if sticker else text,
                is_bot=True,
                reply_to_id=turn.message_id,
                sent_at=sent.get("date", int(now())),
            )
        )
        state_row, state = await session_state(session, turn.group_id, turn.topic_id)
        state.update(last_reply=now(), blocked_until=0)
        store_state(session, state_row, turn.group_id, turn.topic_id, state)
        attention = await session.get(Meta, f"chat_attention:{turn.group_id}")
        activity = json.loads(attention.value) if attention else {}
        activity["last_reply"] = now()
        if attention:
            attention.value = json.dumps(activity)
        else:
            session.add(Meta(key=f"chat_attention:{turn.group_id}", value=json.dumps(activity)))
        if sticker:
            await self.stickers.sent(session, turn.group_id, sticker)
        await capture(
            session,
            {
                "chat": {"id": turn.group_id, "type": group.type, "title": group.title},
                "from": {"id": self.tg.user_id, "is_bot": True, "first_name": name},
                "message_id": sent["message_id"],
                "date": int(now()),
                **({"sticker": sticker} if sticker else {"text": text}),
                "reply_to_message": {"message_id": turn.message_id},
                **({"message_thread_id": turn.topic_id, "is_topic_message": True} if turn.topic_id else {}),
            },
            settings=self.settings,
        )
        source_ids = {turn.message_id}
        saved = json.loads(current.response) if current.response else {}
        for entry in saved.get("source_context", []):
            if entry.get("id") == turn.message_id and entry.get("reply_to"):
                source_ids.add(entry["reply_to"])
        await remember_receipt(session, current, sent["message_id"], source_ids)

