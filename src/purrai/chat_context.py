"""Eligible history, frozen request prefixes and confirmed reply receipts."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, literal, or_, select, union_all

from purrai.shared_policy import effective_rules
from purrai.chat_input import input_reasons
from purrai import chat_commands
from purrai.chat_messages import recent_messages
from purrai.group_link_identity import own_channel_id
from purrai.chat_state import UNREADABLE_IMAGE, blocked_message, ignored_text, image_message_ids, message_bursts, permitted_images, session_state, store_state
from purrai.models import ArchivedMessage, ChatMessage, ChatTurn, Group, MessageRevision, Meta, now
from purrai.schemas import GroupSettings
from purrai.telegram_sources import channel_sources, external_channel_reasons, stored_channel_sources


def latest_payload():
    # Match archive ordering, including out-of-order originals and edits. The
    # archive_id index bounds each lookup to one message's retained revisions.
    return select(MessageRevision.payload).where(MessageRevision.archive_id == ArchivedMessage.id).order_by(
        MessageRevision.event_at.desc(), MessageRevision.is_edit.desc(),
        MessageRevision.update_id.desc(), MessageRevision.id.desc(),
    ).limit(1).correlate(ArchivedMessage).scalar_subquery()


class ChatContext:
    def __init__(self, telegram):
        self.tg = telegram

    async def allowed(self, session, group_id, message_id, sender_id):
        return not await session.scalar(select(blocked_message(group_id, message_id, sender_id)))

    async def recent(self, session, group_id, topic_id):
        return await recent_messages(session, group_id, topic_id)

    async def filter(self, session, group_id, topic_id, context, cfg, *, message_id=0, direct=False):
        """Cached text is reusable only while its sources are still eligible."""
        if not context:
            return []
        _, command_state = await chat_commands.load(session, group_id)
        ignored = cfg.chat_ignored_texts + chat_commands.learned(command_state, cfg)
        speakers = {row["id"]: row["speaker_id"] for row in context}
        for row in context:
            if row.get("reply_to"):
                speakers.setdefault(row["reply_to"], row.get("reply_to_speaker_id", 0))
        recent = await self.recent(session, group_id, topic_id)
        rules = await effective_rules(session, group_id)
        allowed_images = permitted_images(recent, message_id, direct)
        if direct:
            target = next((row for row in recent if row.message_id == message_id), None)
            if target and target.reply_to_id:
                allowed_images.add(target.reply_to_id)
        sources = [row for row in recent if row.message_id in speakers]
        speakers.update({row.message_id: row.sender_id for row in sources})
        for row in sources:
            if row.is_bot and row.reply_to_id:
                speakers.setdefault(row.reply_to_id, 0)
        source_metadata = {row["id"]: row.get("channel_sources", []) for row in context}
        source_metadata.update({row.message_id: stored_channel_sources(row.reply_text) for row in sources})
        # Older chat rows did not retain channel origins. Recover them from the
        # independent archive, including sources already absent from chat's window.
        for identifier, sender, payload in await session.execute(select(
            ArchivedMessage.message_id, ArchivedMessage.sender_id, latest_payload(),
        ).where(ArchivedMessage.group_id == group_id, ArchivedMessage.message_id.in_(speakers))):
            speakers[identifier] = sender or 0
            if payload:
                source_metadata[identifier] = channel_sources(payload)
        linked_channel_id = await own_channel_id(session, group_id)
        # Include frozen entries whose small rolling ChatMessage row has aged out.
        # A single batch checks the existing moderation facts, without an LLM call.
        candidates = union_all(*(select(
            literal(identifier).label("message_id"), literal(sender).label("sender_id"),
        ) for identifier, sender in speakers.items())).subquery()
        excluded = set(await session.scalars(select(candidates.c.message_id).where(
            blocked_message(group_id, candidates.c.message_id, candidates.c.sender_id),
        )))
        excluded.update(row.message_id for row in sources if not row.eligible and not (
            row.message_id in allowed_images and row.text.startswith(UNREADABLE_IMAGE)
        ))
        excluded.update(image_message_ids(recent) - allowed_images)
        excluded.update(identifier for identifier, origins in source_metadata.items() if external_channel_reasons(
            origins, group_id, cfg.model_dump(), rules, linked_channel_id=linked_channel_id,
        ))
        for burst in message_bursts(recent):
            if not any(row.is_bot for row in burst) and input_reasons("\n".join(row.text for row in burst), rules, cfg.model_dump(),
                             direct=direct and any(row.message_id == message_id for row in burst)):
                excluded.update(row.message_id for row in burst)
        records = context + [
            {"id": row.message_id, "text": row.text, "bot": row.is_bot, "reply_to": row.reply_to_id}
            for row in sources
        ]
        excluded.update(row["id"] for row in records if (
            not row["text"].strip() or row["text"].lstrip().startswith("/")
            or ignored_text(row["text"], ignored, getattr(self.tg, "username", ""))
            or not row.get("bot") and chat_commands.known_ad(command_state, row["text"])
            or not row.get("bot") and input_reasons(row["text"], rules, cfg.model_dump(), direct=direct and row["id"] == message_id)
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
            quote = row.get("quote", "")
            if (row.get("reply_to") in excluded or input_reasons(quote, rules, cfg.model_dump())
                    or quote and (ignored_text(quote, ignored, getattr(self.tg, "username", ""))
                                  or chat_commands.known_ad(command_state, quote))):
                row = {key: value for key, value in row.items()
                       if key not in ("reply_to", "quote", "reply_to_speaker_id", "reply_to_name")}
            result.append(row)
        return result

    def clean_conditions(self, group_id, topic_id, allowed_images=()):
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
            or_(ChatMessage.eligible.is_(True), and_(
                ChatMessage.message_id.in_(allowed_images), ChatMessage.text.startswith(UNREADABLE_IMAGE),
            )),
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
        recent = await self.recent(session, turn.group_id, turn.topic_id)
        allowed_images = permitted_images(recent, turn.message_id, turn.direct)
        image_ids = image_message_ids(recent) - allowed_images
        conditions = (
            *self.clean_conditions(turn.group_id, turn.topic_id, allowed_images),
            ChatMessage.sent_at >= now() - 6 * 3600,
            ChatMessage.received_at >= reset_at,
            ChatMessage.message_id.not_in(image_ids),
            ~select(ChatTurn.id).where(
                ChatTurn.group_id == turn.group_id,
                ChatTurn.message_id == ChatMessage.message_id,
                ChatTurn.state == "pending",
                ChatTurn.direct.is_(True), ChatTurn.id != turn.id,
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
        _, command_state = await chat_commands.load(session, turn.group_id)
        ignored = cfg.chat_ignored_texts + chat_commands.learned(command_state, cfg)
        rows = [row for row in rows if not ignored_text(row.text, ignored, getattr(self.tg, "username", ""))][:cfg.chat_history]
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
            origins = stored_channel_sources(row.reply_text)
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
                    **({"channel_sources": origins} if origins else {}),
                    **({"reply_to": row.reply_to_id} if source or row.reply_to_id in visible_ids or row.is_bot else {}),
                    **({"quote": quote} if quote else {}),
                    "time": datetime.fromtimestamp(row.sent_at, ZoneInfo("Asia/Shanghai")).strftime(
                        "%H:%M:%S"
                    ),
                }
            )
        result.reverse()
        for row in selected:
            if not row.reply_to_id:
                continue
            entry = next(item for item in result if item["id"] == row.message_id)
            source = sources.get(row.reply_to_id)
            if source:
                entry.update(reply_to_speaker_id=source.sender_id, reply_to_name=source.sender_name)
                continue
            reference = await self.reference(session, row)
            if reference:
                other_size = sum(len(item["text"]) + len(item.get("quote", ""))
                                 for item in result if item["id"] != row.message_id)
                if "quote" in reference:
                    reference["quote"] = reference["quote"][:max(0, 18000 - other_size - len(entry["text"]))]
                entry.update(reference)
        target = next((row for row in selected if row.message_id == turn.message_id), None)
        if target and turn.direct:
            entry = next(row for row in result if row["id"] == turn.message_id)
            if turn.message_id in image_message_ids(recent):
                entry["image_unavailable"] = True
        combined = await self.filter(session, turn.group_id, turn.topic_id, history + result, cfg,
                                     message_id=turn.message_id, direct=turn.direct)
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

    async def reference(self, session, target):
        """Supply factual reply identity, plus eligible explicit image/channel text."""
        try:
            snapshot = json.loads(target.reply_text)
            if snapshot.get("version") != 1 or snapshot.get("message_id") != target.reply_to_id:
                snapshot = {}
        except (ValueError, TypeError, AttributeError):
            snapshot = {}
        archived = await session.scalar(select(ArchivedMessage).where(
            ArchivedMessage.group_id == target.group_id, ArchivedMessage.message_id == target.reply_to_id,
            ArchivedMessage.source == "telegram",
        ))
        sender_id = archived.sender_id if archived else snapshot.get("sender_id", 0)
        sender_name = archived.sender_name if archived else snapshot.get("sender_name", "")
        if not snapshot and not archived:
            return {}
        if not await self.allowed(
            session, target.group_id, target.reply_to_id, sender_id,
        ):
            return {}
        include_text = "text" in snapshot and not (archived and archived.sender_type == "bot")
        text = ((archived.text or archived.caption) if archived else snapshot["text"]) if include_text else ""
        group = await session.get(Group, target.group_id)
        rules = await effective_rules(session, target.group_id)
        origins = snapshot.get("reference_sources", [])
        if archived:
            payload = await session.scalar(select(latest_payload()).select_from(ArchivedMessage).where(
                ArchivedMessage.id == archived.id,
            ))
            if payload:
                origins = channel_sources(payload)
        if external_channel_reasons(origins, target.group_id, group.settings, rules,
                                    linked_channel_id=await own_channel_id(session, target.group_id)):
            return {}
        if input_reasons(text, rules, group.settings):
            text = ""
        image = include_text and bool(archived and archived.message_type == "photo" or snapshot.get("image_unavailable"))
        return {"reply_to": target.reply_to_id,
                **({"reply_to_speaker_id": sender_id} if sender_id else {}),
                **({"reply_to_name": sender_name} if sender_name else {}),
                **({"quote": text[:1600]} if include_text else {}),
                **({"image_unavailable": True} if image else {})}

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
            valid = context == await self.filter(session, turn.group_id, turn.topic_id, context, cfg,
                                                message_id=turn.message_id, direct=turn.direct)
        return current, group, valid
