"""Confirm disappearance of replied-to user messages without posting a probe."""

import asyncio
import json
import logging
from time import monotonic

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError
from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.chat_retraction import RECEIPT_RETENTION, receipt, source_deleted
from purrai.deletion_effects import deletion_result
from purrai.models import ArchivedMessage, ChatTurn, Group, Meta, now
from purrai.telegram_health import request_purpose

logger = logging.getLogger(__name__)
KEY = "chat_source_checks"
CONFIRM_DELAY = 30
REVISIT = 300
MAX_TRACKED = 5000
REQUEST_TIMEOUT = 10
REQUEST_SPACING = 1
SWEEP_BUDGET = 30
RETRY_BASE = 30
RETRY_MAX = 900


class ReplySourceChecks:
    def __init__(self, db, telegram):
        self.db, self.tg = db, telegram
        self.lock = asyncio.Lock()
        self.state = {"items": {}}
        self.next_request = 0

    def status(self):
        entries = list(self.state.get("items", {}).values())
        delayed = [entry for entry in entries if entry.get("failures")]
        return {"delayed": len(delayed), "repeated": sum(entry["failures"] > 1 for entry in delayed),
                "first_failure": min((entry["first_failure"] for entry in delayed), default=0),
                "last_failure": max((entry["at"] for entry in delayed), default=0),
                "next_retry_at": max(self.state.get("pause_until", 0),
                                     min((entry["next"] for entry in delayed), default=0)) if delayed else 0,
                "last_success": self.state.get("last_success", 0)}

    async def restore(self):
        async with self.db.sessions() as session:
            row = await session.get(Meta, KEY)
            self.state = json.loads(row.value) if row else {"items": {}}
            await self.candidates(session, self.state)

    async def call(self, method, **kwargs):
        await asyncio.sleep(max(0, self.next_request - monotonic()))
        self.next_request = monotonic() + REQUEST_SPACING
        token = request_purpose.set("source_check")
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT + 1):
                return await self.tg.call(method, **kwargs, request_timeout=REQUEST_TIMEOUT)
        finally:
            request_purpose.reset(token)

    async def probe(self, group_id, message_id):
        # Only ordinary user messages and our own keyboard-free chat replies
        # are candidates. Removing a nonexistent keyboard does not alter them.
        try:
            await self.call("edit_message_reply_markup", chat_id=group_id, message_id=message_id, reply_markup=None)
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
        result, active = [], set()
        for turn in turns:
            saved = receipt(turn)
            source = archived.get((turn.group_id, turn.message_id))
            checkable = turn.message_id in saved.get("checkable_source_ids", []) or bool(
                source and source.sender_type == "user" and source.sender_id and source.sender_id > 0
            )
            if not checkable:
                continue
            active.add(str(turn.id))
            entry = state.get("items", {}).get(str(turn.id), {})
            if entry.get("next", 0) > now():
                continue
            controls = sorted(set(saved.get("sent_message_ids", [])) | {turn.sent_message_id})
            result.append({"turn_id": turn.id, "group_id": turn.group_id, "message_id": turn.message_id,
                           "controls": controls[:2], "next": entry.get("next", 0)})
        state["items"] = {key: value for key, value in state.get("items", {}).items() if key in active}
        return sorted(result, key=lambda item: (item["next"], item["turn_id"]))

    async def store(self, candidate, verdict, *, pause=0, error=""):
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
            failures = before.get("failures", 0) + 1 if verdict == "unknown" else 0
            delay = min(RETRY_MAX, RETRY_BASE * 2 ** min(failures - 1, 5)) if failures else (
                CONFIRM_DELAY if verdict == "absent" else REVISIT)
            entries[key] = {"at": now(), "next": now() + max(delay, pause),
                            "missing_since": (first_missing or now()) if verdict == "absent" else 0,
                            "failures": failures, "first_failure": (before.get("first_failure") or now()) if failures else 0,
                            "error": error if failures else ""}
            if not failures:
                state["last_success"] = now()
            if confirmed or not group or not group.approved or not turn or turn.state in ("retracted", "retracting", "retraction_failed"):
                entries.pop(key, None)
            state["items"] = dict(sorted(((key, value) for key, value in entries.items()
                if value.get("at", 0) >= now() - RECEIPT_RETENTION),
                key=lambda item: item[1]["at"], reverse=True)[:MAX_TRACKED])
            state["pause_until"] = max(state.get("pause_until", 0), now() + pause) if pause else state.get("pause_until", 0)
            value = json.dumps(state)
            if row:
                row.value = value
            else:
                session.add(Meta(key=KEY, value=value))
            self.state = state
            return bool(confirmed)

    async def sweep(self, limit=10):
        if not self.tg.connected or not self.tg.user_id:
            return 0
        async with self.lock:
            async with self.db.transaction() as session:
                row = await session.get(Meta, KEY)
                state = json.loads(row.value) if row else {"items": {}}
                candidates = (await self.candidates(session, state))[:limit]
                self.state = state
                if row:
                    row.value = json.dumps(state)
            if state.get("pause_until", 0) > now():
                return 0
            confirmed, groups = 0, {}
            started = monotonic()
            for candidate in candidates:
                if monotonic() - started >= SWEEP_BUDGET:
                    break
                verdict, pause, error = "unknown", 0, "inconclusive"
                try:
                    group_id = candidate["group_id"]
                    if group_id not in groups:
                        member = await self.call("get_chat_member", chat_id=group_id, user_id=self.tg.user_id)
                        groups[group_id] = member.get("status") in ("creator", "administrator")
                    if groups[group_id]:
                        for identifier in candidate["controls"]:
                            if await self.probe(group_id, identifier) == "present":
                                verdict = await self.probe(group_id, candidate["message_id"])
                                break
                except TelegramRetryAfter as exc:
                    pause = exc.retry_after
                    error = "rate_limited"
                except (TelegramNetworkError, TimeoutError, ConnectionError, OSError) as exc:
                    error = type(exc).__name__
                except Exception as exc:
                    error = type(exc).__name__
                confirmed += await self.store(candidate, verdict, pause=pause, error=error)
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
