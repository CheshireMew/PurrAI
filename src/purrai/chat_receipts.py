"""Persist confirmed chat output, conversation activity and its source receipts."""

import json

from sqlalchemy import select

from purrai.archive import capture
from purrai.chat_messages import recent_messages
from purrai.chat_retraction import remember_receipt
from purrai.chat_state import following_up, message_bursts, session_state, store_state
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
        target = await session.scalar(select(ChatMessage).where(
            ChatMessage.group_id == turn.group_id, ChatMessage.message_id == turn.message_id,
        ))
        target_burst = next((
            {row.message_id for row in burst}
            for burst in message_bursts(await recent_messages(session, turn.group_id, turn.topic_id))
            if any(row.message_id == turn.message_id for row in burst)
        ), {turn.message_id})
        if state.get("last_request") == turn.message_id:
            discussing_ads = state.get("ad_conversation", False)
        else:
            discussing_ads = bool(target and (
                turn.direct and "广告" in target.text
                or not turn.direct and state.get("ad_conversation")
                and following_up(state, sender_id, target.received_at, message_id=turn.message_id, burst_ids=target_burst)
            ))
        state.update(last_reply=now(), last_user=sender_id, last_request=turn.message_id,
                     blocked_until=0, ad_conversation=discussing_ads)
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

