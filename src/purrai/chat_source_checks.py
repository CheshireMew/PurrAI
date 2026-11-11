"""Confirm disappearance of replied-to user messages without posting a probe."""

import asyncio
import json
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.chat_retraction import RECEIPT_RETENTION, receipt, source_deleted
from purrai.deletion_effects import deletion_result
from purrai.models import ArchivedMessage, ChatTurn, Group, Meta, now

logger = logging.getLogger(__name__)
KEY = "chat_source_checks"
CONFIRM_DELAY = 30
REVISIT = 300
MAX_TRACKED = 5000


class ReplySourceChecks:
    def __init__(self, db, telegram):
        self.db, self.tg = db, telegram
        self.lock = asyncio.Lock()

    async def probe(self, group_id, message_id):
        # Only ordinary user messages and our own keyboard-free chat replies
        # are candidates. Removing a nonexistent keyboard does not alter them.
        try:
            await self.tg.call("edit_message_reply_markup", chat_id=group_id, message_id=message_id,
                               reply_markup=None, request_timeout=3)
            return "present"
        except TelegramBadRequest as exc:
            error = exc.message.casefold()
            if "message to edit not found" in error:
                return "absent"
            if "message is not modified" in error or "message can't be edited" in error:
                return "present"
            return "unknown"

    async def candidates(self, session, state):
        turns = (await session.scalars(select(ChatTurn).join(Group, Group.id == ChatTurn.group_id).where(
            Group.approved.is_(True), ChatTurn.sent_message_id.is_not(None),
            ChatTurn.created_at >= now() - RECEIPT_RETENTION,
            ChatTurn.state.not_in(("retracted", "retracting", "retraction_failed")),
        ).order_by(ChatTurn.id.desc()).limit(MAX_TRACKED))).all()
        archived = {}
        keys = {(turn.group_id, turn.message_id) for turn in turns}
        for group_id in {group_id for group_id, _ in keys}:
            identifiers = [identifier for group, identifier in keys if group == group_id]
            for row in await session.scalars(select(ArchivedMessage).where(
                ArchivedMessage.group_id == group_id, ArchivedMessage.message_id.in_(identifiers),
            )):
                archived[(group_id, row.message_id)] = row
        result = []
        for turn in turns:
            saved = receipt(turn)
            source = archived.get((turn.group_id, turn.message_id))
            checkable = turn.message_id in saved.get("checkable_source_ids", []) or bool(
                source and source.sender_type == "user" and source.sender_id and source.sender_id > 0
            )
            if not checkable:
                continue
            entry = state.get("items", {}).get(str(turn.id), {})
            if entry.get("next", 0) > now():
                continue
            controls = sorted(set(saved.get("sent_message_ids", [])) | {turn.sent_message_id})
            result.append({"turn_id": turn.id, "group_id": turn.group_id, "message_id": turn.message_id,
                           "controls": controls[:2], "next": entry.get("next", 0)})
        return sorted(result, key=lambda item: (item["next"], item["turn_id"]))

    async def store(self, candidate, verdict, *, pause=0):
        async with self.db.transaction() as session:
            row = await session.get(Meta, KEY)
            state = json.loads(row.value) if row else {"items": {}}
            entries = state.setdefault("items", {})
            key = str(candidate["turn_id"])
            before = entries.get(key, {})
            first_missing = before.get("missing_since", 0) if verdict == "absent" else 0
            confirmed = verdict == "absent" and first_missing and now() - first_missing >= CONFIRM_DELAY
            group = await session.get(Group, candidate["group_id"])
            turn = await session.get(ChatTurn, candidate["turn_id"])
            if confirmed and group and group.approved and turn and turn.message_id == candidate["message_id"]:
                if not await source_deleted(session, group.id, turn.message_id):
                    action = await enqueue(session, "delete", f"confirmed-absent:{group.id}:{turn.message_id}", group.id,
                        payload={"message_id": turn.message_id, "already_absent": True},
                        reason="两次核对确认原消息已不存在，仅记录删除事实")
                    action.state, action.updated_at = "succeeded", now()
                    await deletion_result(session, action)
            entries[key] = {"at": now(), "next": now() + (CONFIRM_DELAY if verdict == "absent" else REVISIT),
                            "missing_since": first_missing or now() if verdict == "absent" else 0}
            state["items"] = dict(sorted(((key, value) for key, value in entries.items()
                if value.get("at", 0) >= now() - RECEIPT_RETENTION),
                key=lambda item: item[1]["at"], reverse=True)[:MAX_TRACKED])
            state["pause_until"] = max(state.get("pause_until", 0), now() + pause) if pause else state.get("pause_until", 0)
            value = json.dumps(state)
            if row:
                row.value = value
            else:
                session.add(Meta(key=KEY, value=value))
            return bool(confirmed)

    async def sweep(self, limit=10):
        if not self.tg.connected or not self.tg.user_id:
            return 0
        async with self.lock:
            async with self.db.sessions() as session:
                row = await session.get(Meta, KEY)
                state = json.loads(row.value) if row else {"items": {}}
                if state.get("pause_until", 0) > now():
                    return 0
                candidates = (await self.candidates(session, state))[:limit]
            confirmed, groups = 0, {}
            for candidate in candidates:
                verdict, pause = "unknown", 0
                try:
                    group_id = candidate["group_id"]
                    if group_id not in groups:
                        member = await self.tg.call("get_chat_member", chat_id=group_id,
                                                    user_id=self.tg.user_id, request_timeout=3)
                        groups[group_id] = member.get("status") in ("creator", "administrator")
                    if groups[group_id]:
                        for identifier in candidate["controls"]:
                            if await self.probe(group_id, identifier) == "present":
                                verdict = await self.probe(group_id, candidate["message_id"])
                                break
                except TelegramRetryAfter as exc:
                    pause = exc.retry_after
                except Exception as exc:
                    logger.warning("原消息存在性核对暂未完成 turn=%s error=%s", candidate["turn_id"], type(exc).__name__)
                confirmed += await self.store(candidate, verdict, pause=pause)
                if pause:
                    break
            return confirmed

    async def run(self):
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("原消息回溯暂未完成 error=%s", type(exc).__name__)
            await asyncio.sleep(15)
