"""Eligible history, frozen request prefixes and confirmed reply receipts."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, literal, or_, select, union_all

from purrai.archive import capture
from purrai.chat_state import blocked_message, following_up, ignored_text, image_message_ids, message_bursts, session_state, store_state
from purrai.models import ChatMessage, ChatTurn, Group, Meta, now
from purrai.schemas import GroupSettings
from purrai.stickers import sticker_text


class ChatContext:
    def __init__(self, telegram, settings, stickers):
        self.tg, self.settings, self.stickers = telegram, settings, stickers

    async def allowed(self, session, group_id, message_id, sender_id):
        return not await session.scalar(select(blocked_message(group_id, message_id, sender_id)))

    async def recent(self, session, group_id, topic_id):
        return (await session.scalars(select(ChatMessage).where(
            ChatMessage.group_id == group_id, ChatMessage.topic_id == topic_id,
        ).order_by(ChatMessage.sent_at.desc(), ChatMessage.message_id.desc()).limit(100))).all()

    async def filter(self, session, group_id, topic_id, context, cfg):
        """Cached text is reusable only while its sources are still eligible."""
        if not context:
            return []
        speakers = {row["id"]: row["speaker_id"] for row in context}
        for row in context:
            if row.get("reply_to"):
                speakers.setdefault(row["reply_to"], 0)
        recent = await self.recent(session, group_id, topic_id)
        sources = [row for row in recent if row.message_id in speakers]
        speakers.update({row.message_id: row.sender_id for row in sources})
        for row in sources:
            if row.is_bot and row.reply_to_id:
                speakers.setdefault(row.reply_to_id, 0)
        # Include frozen entries whose small rolling ChatMessage row has aged out.
        # A single batch checks the existing moderation facts, without an LLM call.
        candidates = union_all(*(select(
            literal(identifier).label("message_id"), literal(sender).label("sender_id"),
        ) for identifier, sender in speakers.items())).subquery()
        excluded = set(await session.scalars(select(candidates.c.message_id).where(
            blocked_message(group_id, candidates.c.message_id, candidates.c.sender_id),
        )))
        excluded.update(row.message_id for row in sources if not row.eligible)
        excluded.update(image_message_ids(recent))
        records = context + [
            {"id": row.message_id, "text": row.text, "bot": row.is_bot, "reply_to": row.reply_to_id}
            for row in sources
        ]
        excluded.update(row["id"] for row in records if (
            not row["text"].strip() or row["text"].lstrip().startswith("/")
            or ignored_text(row["text"], cfg.chat_ignored_texts)
        ))
        # Do not keep our old commentary on removed content as a fresh topic.
        while True:
            replies = {row["id"] for row in records if row.get("bot") and row.get("reply_to") in excluded}
            if replies <= excluded:
                break
            excluded.update(replies)
        result = []
        for row in context:
            if row["id"] in excluded:
                continue
            if row.get("reply_to") in excluded:
                row = {key: value for key, value in row.items() if key not in ("reply_to", "quote")}
            result.append(row)
        return result

    def clean_conditions(self, group_id, topic_id):
        own_reply = (
            select(ChatTurn.id)
            .where(
                ChatTurn.group_id == group_id,
                ChatTurn.topic_id == topic_id,
                ChatTurn.sent_message_id.is_not(None),
                ChatTurn.message_id == ChatMessage.reply_to_id,
            )
            .exists()
        )
        return (
            ChatMessage.group_id == group_id,
            ChatMessage.topic_id == topic_id,
            ChatMessage.eligible.is_(True),
            ChatMessage.text != "",
            ~ChatMessage.text.startswith("/"),
            ~blocked_message(group_id, ChatMessage.message_id, ChatMessage.sender_id),
            or_(
                ChatMessage.is_bot.is_(False),
                and_(
                    ChatMessage.sender_id == self.tg.user_id,
                    own_reply,
                ),
            ),
        )

    async def build(self, session, turn, cfg):
        reset = await session.get(Meta, f"chat_reset:{turn.group_id}")
        reset_at = float(reset.value) if reset else 0
        _, state = await session_state(session, turn.group_id, turn.topic_id)
        continuing = (
            state.get("context_reset", 0) == reset_at and now() - state.get("context_started", 0) < 6 * 3600
        )
        history = state.get("sent_context", []) if continuing else []
        history_ids = {row["id"] for row in history}
        cursor = state.get("context_cursor", 0) if continuing else 0
        image_ids = image_message_ids(await self.recent(session, turn.group_id, turn.topic_id))
        conditions = (
            *self.clean_conditions(turn.group_id, turn.topic_id),
            ChatMessage.sent_at >= now() - 6 * 3600,
            ChatMessage.received_at >= reset_at,
            ChatMessage.message_id.not_in(image_ids),
            ~select(ChatTurn.id).where(
                ChatTurn.group_id == turn.group_id,
                ChatTurn.message_id == ChatMessage.message_id,
                ChatTurn.state == "pending",
                or_(ChatTurn.available_at > now(), and_(ChatTurn.direct.is_(True), ChatTurn.id != turn.id)),
            ).exists(),
        )
        rows = (
            await session.scalars(
                select(ChatMessage)
                .where(
                    *conditions,
                    ChatMessage.id > cursor,
                    ChatMessage.message_id.not_in(history_ids),
                )
                .order_by(ChatMessage.id.desc())
                .limit(80)
            )
        ).all()
        rows = [row for row in rows if not ignored_text(row.text, cfg.chat_ignored_texts)][:cfg.chat_history]
        source_ids = {row.reply_to_id for row in rows if row.reply_to_id}
        sources = (
            {
                row.message_id: row
                for row in (
                    await session.scalars(
                        select(ChatMessage).where(*conditions, ChatMessage.message_id.in_(source_ids))
                    )
                ).all()
            }
            if source_ids
            else {}
        )
        selected, size = [], 0
        for row in rows:
            if size + len(row.text) > 18000:
                break
            selected.append(row)
            size += len(row.text)
        visible_ids = {row["id"] for row in history} | {row.message_id for row in selected}
        result = []
        for row in selected:
            source = sources.get(row.reply_to_id)
            quote = source.text[:500] if source and source.message_id not in visible_ids else ""
            if size + len(quote) > 18000:
                quote = ""
            size += len(quote)
            result.append(
                {
                    "id": row.message_id,
                    "speaker_id": row.sender_id,
                    "name": row.sender_name,
                    "bot": row.is_bot,
                    "text": row.text,
                    **({"reply_to": row.reply_to_id} if source or row.reply_to_id in visible_ids or row.is_bot else {}),
                    **({"quote": quote} if quote else {}),
                    "time": datetime.fromtimestamp(row.sent_at, ZoneInfo("Asia/Shanghai")).strftime(
                        "%H:%M:%S"
                    ),
                }
            )
        result.reverse()
        combined = await self.filter(session, turn.group_id, turn.topic_id, history + result, cfg)
        history = [row for row in combined if row["id"] in history_ids]
        result = [row for row in combined if row["id"] not in history_ids]
        # Preserve eligible sent content; moderation takes priority over prefix reuse.
        # At the bound, rotate a batch instead of shifting the prefix on every message.
        while history and (
            len(history) + len(result) > cfg.chat_history
            or sum(len(row["text"]) + len(row.get("quote", "")) for row in history + result) > 18000
        ):
            history = history[(len(history) + 1) // 2 :]
        return history + result

    async def remember_sent_context(self, session, turn, context):
        row, state = await session_state(session, turn.group_id, turn.topic_id)
        reset = await session.get(Meta, f"chat_reset:{turn.group_id}")
        reset_at = float(reset.value) if reset else 0
        if state.get("context_reset", 0) != reset_at or now() - state.get("context_started", 0) >= 6 * 3600:
            state["context_started"] = now()
        cursor = await session.scalar(select(func.max(ChatMessage.id)).where(
            ChatMessage.group_id == turn.group_id, ChatMessage.topic_id == turn.topic_id,
        )) or 0
        waiting = await session.scalar(select(func.min(ChatMessage.id)).join(ChatTurn, and_(
            ChatTurn.group_id == ChatMessage.group_id, ChatTurn.message_id == ChatMessage.message_id,
        )).where(
            ChatMessage.group_id == turn.group_id, ChatMessage.topic_id == turn.topic_id,
            ChatTurn.state == "pending",
        ))
        state.update(
            sent_context=context,
            context_reset=reset_at,
            # An unready message has not been sent to the model yet. Keep it
            # beyond the cursor; already-frozen entries are deduplicated above.
            context_cursor=min(cursor, waiting - 1) if waiting is not None else cursor,
        )
        store_state(session, row, turn.group_id, turn.topic_id, state)

    async def remember_output(self, session, turn, cfg, group, sent, sender_id, *, text=None, sticker=None):
        current = await session.get(ChatTurn, turn.id)
        current.sent_message_id = sent["message_id"]
        session.add(
            ChatMessage(
                group_id=turn.group_id,
                topic_id=turn.topic_id,
                message_id=sent["message_id"],
                sender_id=self.tg.user_id,
                sender_name=cfg.chat_name,
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
            for burst in message_bursts(await self.recent(session, turn.group_id, turn.topic_id))
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
                "from": {"id": self.tg.user_id, "is_bot": True, "first_name": cfg.chat_name},
                "message_id": sent["message_id"],
                "date": int(now()),
                **({"sticker": sticker} if sticker else {"text": text}),
                "reply_to_message": {"message_id": turn.message_id},
                **({"message_thread_id": turn.topic_id, "is_topic_message": True} if turn.topic_id else {}),
            },
            settings=self.settings,
        )

    async def valid_turn(self, session, turn, original, sender_id, context):
        current = await session.get(ChatTurn, turn.id)
        group = await session.get(Group, turn.group_id)
        target = await session.scalar(
            select(ChatMessage).where(
                ChatMessage.group_id == turn.group_id,
                ChatMessage.message_id == turn.message_id,
            )
        )
        reset = await session.get(Meta, f"chat_reset:{turn.group_id}")
        valid = (
            current.state == "generating"
            and group.approved
            and group.settings.get("chat_enabled")
            and target
            and target.eligible
            and target.text == original
            and now() - target.sent_at <= 120
            and now() - turn.created_at <= 120
            and not (reset and float(reset.value) > turn.created_at)
            and await self.allowed(session, turn.group_id, turn.message_id, sender_id)
        )
        if valid:
            cfg = GroupSettings.model_validate(group.settings)
            valid = context == await self.filter(session, turn.group_id, turn.topic_id, context, cfg)
        return current, group, valid
