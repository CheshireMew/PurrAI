"""Coalesce cosmetic CAPTCHA edits without holding the database writer on HTTP."""

import asyncio
import hashlib
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from purrai import captcha
from purrai.diagnostics import failure
from purrai.models import Action, Group, Member, Meta, Verification, now, uid
from purrai.telegram import is_admin

DEBOUNCE = 0.12
EDIT_INTERVAL = 1.0
logger = logging.getLogger(__name__)


class VerificationProgress:
    def __init__(self, db, telegram, settings, exempt_administrator):
        self.db, self.tg = db, telegram
        self.settings = settings
        self.exempt_administrator = exempt_administrator
        self.lock = asyncio.Lock()

    async def queue(self, session, verification):
        key = f"progress:{verification.id}:{verification.nonce}"
        action = await session.scalar(select(Action).where(Action.dedupe_key == key))
        if action is None:
            session.add(
                Action(
                    id=uid(),
                    kind="progress",
                    dedupe_key=key,
                    group_id=verification.group_id,
                    user_id=verification.user_id,
                    payload={"verification_id": verification.id, "expected_nonce": verification.nonce},
                    priority=2,
                    state="pending",
                    attempts=0,
                    available_at=now() + DEBOUNCE,
                    error="",
                )
            )
        elif action.state != "pending":
            action.state, action.attempts, action.error = "pending", 0, ""
            action.available_at = max(now() + DEBOUNCE, action.payload.get("rendered_at", 0) + EDIT_INTERVAL)
            action.updated_at = now()
        # A pending retry keeps its deadline: new digits never bypass Telegram's cooldown.

    @staticmethod
    async def snapshot(session, action):
        verification = await session.get(Verification, action.payload.get("verification_id"))
        if not verification or verification.state != "pending" or not verification.message_id or verification.choices:
            return None
        member = await session.get(Member, (verification.group_id, verification.user_id))
        if (
            not member
            or member.generation != verification.generation
            or action.payload.get("expected_nonce") not in (None, verification.nonce)
        ):
            return None
        group = await session.get(Group, verification.group_id)
        if not group or not group.approved:
            return None
        caption = captcha.caption(
            verification.user_name,
            verification.input_digits,
            verification.attempts,
            group.settings["max_attempts"],
            verification.deadline,
            join_request=verification.flow == "join_request",
            group_title=group.title,
            failure_action=group.settings.get("verification_failure_action", "permanent_mute"),
            multiple_choice=bool(verification.choices),
        )
        return {
            "group_id": group.id,
            "user_id": member.user_id,
            "check_role": member.present,
            "chat_id": verification.prompt_chat_id or group.id,
            "message_id": verification.message_id,
            "nonce": verification.nonce,
            "caption": caption,
            "hash": hashlib.sha256(caption.encode()).hexdigest(),
            "reply_markup": captcha.keyboard(verification.id, verification.nonce, verification.choices),
        }

    async def run_one(self, identifier=None):
        async with self.lock:
            async with self.db.transaction() as session:
                query = select(Action).where(
                    Action.kind == "progress", Action.state == "pending", Action.available_at <= now()
                )
                if identifier:
                    query = query.where(Action.id == identifier)
                action = await session.scalar(query.order_by(Action.created_at).limit(1))
                if action is None:
                    return False
                snapshot = await self.snapshot(session, action)
                if snapshot is None:
                    action.state, action.updated_at = "cancelled", now()
                    return True
                # Also compact tasks left by an older deployed version on restart.
                duplicates = await session.scalars(
                    select(Action).where(
                        Action.kind == "progress",
                        Action.state == "pending",
                        Action.id != action.id,
                        Action.payload["verification_id"].as_string() == action.payload["verification_id"],
                    )
                )
                for duplicate in duplicates:
                    duplicate.state, duplicate.updated_at = "cancelled", now()
                cooldown_key = f"progress-cooldown:{snapshot['chat_id']}"
                cooldown = await session.get(Meta, cooldown_key)
                if cooldown and float(cooldown.value) > now():
                    action.available_at = float(cooldown.value)
                    return True
                if action.payload.get("rendered_hash") == snapshot["hash"]:
                    action.state, action.error, action.updated_at = "succeeded", "", now()
                    return True
                identifier = action.id
                action.attempts += 1
                # Commit the attempt before HTTP; the pending action is restartable.

            error = None
            try:
                if snapshot["check_role"] and is_admin(await self.tg.call(
                    "get_chat_member", chat_id=snapshot["group_id"], user_id=snapshot["user_id"], request_timeout=3,
                )):
                    async with self.db.transaction() as session:
                        await self.exempt_administrator(session, snapshot["group_id"], snapshot["user_id"])
                    return True
                await self.tg.call(
                    "edit_message_caption",
                    chat_id=snapshot["chat_id"],
                    message_id=snapshot["message_id"],
                    caption=snapshot["caption"],
                    reply_markup=snapshot["reply_markup"],
                    request_timeout=3,
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc):
                    error = exc
            except Exception as exc:
                error = exc

            async with self.db.transaction() as session:
                action = await session.get(Action, identifier)
                current = await self.snapshot(session, action)
                if action.state != "pending":
                    return True
                if current is None or any(
                    current[k] != snapshot[k] for k in ("nonce", "chat_id", "message_id")
                ):
                    action.state = "cancelled"
                elif isinstance(error, TelegramRetryAfter):
                    failure(logger, self.settings, error, "验证进度刷新限流", f"action={identifier}")
                    action.available_at = now() + error.retry_after
                    action.error = f"Telegram 限流，{error.retry_after} 秒后重试"
                    cooldown = await session.get(Meta, cooldown_key)
                    if cooldown:
                        cooldown.value = str(max(float(cooldown.value), action.available_at))
                    else:
                        session.add(Meta(key=cooldown_key, value=str(action.available_at)))
                elif isinstance(error, (TelegramBadRequest, TelegramForbiddenError)):
                    action.state = "failed"
                    action.error = failure(logger, self.settings, error, "验证进度刷新失败", f"action={identifier}", terminal=True)
                elif error:
                    action.available_at = now() + min(30, 0.5 * 2 ** min(action.attempts - 1, 6))
                    action.error = failure(logger, self.settings, error, "验证进度刷新重试", f"action={identifier}", terminal=action.attempts >= 8)
                    if action.attempts >= 8:
                        action.state = "failed"
                else:
                    action.payload = {
                        **action.payload,
                        "rendered_hash": snapshot["hash"],
                        "rendered_at": now(),
                    }
                    action.error = ""
                    if current["hash"] == snapshot["hash"]:
                        action.state = "succeeded"
                    else:
                        action.available_at = now() + EDIT_INTERVAL
                action.updated_at = now()
            return True

    async def loop(self):
        while True:
            try:
                if not self.tg.connected or not self.tg.user_id or not await self.run_one():
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Only the cosmetic edit is delayed; durable input and release keep running.
                failure(logger, self.settings, exc, "验证进度循环异常", terminal=True)
                await asyncio.sleep(1)
