"""Telegram effects of verification decisions."""

import logging
from time import perf_counter

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from purrai import captcha
from purrai.action_queue import enqueue
from purrai.action_execution import ActionPreflightError
from purrai.models import (
    now,
)
from purrai.telegram import is_present
from purrai.verification_cleanup import cleanup

logger = logging.getLogger(__name__)


class VerificationActions:
    def __init__(self, transport, verification, restrictions, administration):
        self.action_call, self.verification = transport.call, verification
        self.restrictions, self.administration = restrictions, administration

    async def mute(self, session, action, group, verification, member):
        await self.restrictions.verification_mute(session, action, group, verification, member)
        await enqueue(
            session,
            "challenge",
            f"challenge:{verification.id}:{verification.nonce}",
            group.id,
            member.user_id,
            {"verification_id": verification.id},
            1,
        )

    async def challenge(self, session, action, group, verification, member):
        if action.payload.get("challenge_delivery") in ("sending", "uncertain"):
            verification.state = "pending"
            action.payload = {**action.payload, "challenge_delivery": "uncertain"}
            raise ValueError("验证码发送结果未确认，未重复出题；原图片仍可验证，也可请管理员重新出题")
        await cleanup(session, verification)
        prepared = now()
        generation_started = perf_counter()
        code, image = captcha.make_image()
        verification.answer_hash = captcha.digest(code, verification.nonce)
        verification.input_digits = ""
        verification.choices = captcha.make_choices(code)
        verification.message_id = None
        # Persist before Telegram I/O. A lost receipt or restart must not send a
        # second picture and forget the first one's message ID.
        started = now()
        attempt = action.payload.get("timing", {})
        timing = {
            **attempt,
            "preparation_seconds": round(max(0, prepared - attempt.get("attempt_started_at", prepared)), 3),
            "image_seconds": round(perf_counter() - generation_started, 3),
            "send_started_at": started,
        }
        action.payload = {**action.payload, "challenge_delivery": "preparing",
                          "challenge_nonce": verification.nonce, "timing": timing}
        send_started = None

        def sending():
            nonlocal send_started
            send_started = perf_counter()
            timing["send_started_at"] = now()
            action.payload = {**action.payload, "challenge_delivery": "sending", "timing": dict(timing)}

        try:
            response = await self.action_call(
                session,
                action,
                "send_photo",
                before_send=sending,
                request_timeout=30,
                chat_id=verification.prompt_chat_id or group.id,
                photo=image,
                caption=captcha.caption(
                    verification.user_name,
                    "",
                    verification.attempts,
                    group.settings["max_attempts"],
                    verification.deadline,
                    join_request=verification.flow == "join_request",
                    group_title=group.title,
                    failure_action=group.settings.get("verification_failure_action", "permanent_mute"),
                    multiple_choice=bool(verification.choices),
                ),
                reply_markup=captcha.keyboard(verification.id, verification.nonce, verification.choices),
            )
        except (TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError, ActionPreflightError):
            self.delivery_result(action, timing, send_started, "rejected")
            raise
        except (TelegramNetworkError, TimeoutError, ConnectionError, OSError) as exc:
            verification.state = "pending"
            self.delivery_result(action, timing, send_started, "uncertain")
            raise ValueError("验证码发送结果未确认，未重复出题；原图片仍可验证，也可请管理员重新出题") from exc
        self.delivery_result(action, timing, send_started, "confirmed")
        verification.message_id = response["message_id"]
        verification.state = "pending"
        verification.error = ""

    @staticmethod
    def delivery_result(action, timing, started, delivery):
        elapsed = round(perf_counter() - started, 3) if started is not None else 0
        timing = {**timing, "send_seconds": elapsed, "send_finished_at": now()}
        action.payload = {**action.payload, "challenge_delivery": delivery, "timing": timing}
        if elapsed >= 3:
            logger.warning(
                "验证码图片发送较慢 action=%s group=%s queue=%.3fs prepare=%.3fs image=%.3fs send=%.3fs delivery=%s",
                action.id, action.group_id, timing.get("queue_seconds", 0),
                timing["preparation_seconds"], timing["image_seconds"], elapsed, delivery,
            )

    async def release(self, session, action, group, verification, member):
        if verification and verification.flow == "join_request":
            return await self.approve(session, action, group, verification, member)
        return await self.administration.restore(session, action, group, verification, member)

    async def approve(self, session, action, group, verification, member):
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id
        )
        if not is_present(current):
            await self.action_call(
                session, action, "approve_chat_join_request", chat_id=group.id, user_id=action.user_id
            )
        verification.state, verification.error, verification.updated_at = "passed", "", now()
        member.passed_at, member.request_until = now(), 0
        if is_present(current):
            verification.previous_permissions = {
                **verification.previous_permissions,
                "entry_consumed": True,
            }
            member.present = True
        await cleanup(session, verification)

    async def fail(self, session, action, group, verification, member):
        if verification and verification.flow == "join_request":
            return await self.decline(session, action, group, verification, member)
        if action.kind == "verification_failure":
            return await self.apply_failure(session, action, group, verification, member)
        return await self.administration.ban(session, action, group, verification, member)

    async def decline(self, session, action, group, verification, member):
        await self.action_call(
            session, action, "decline_chat_join_request", chat_id=group.id, user_id=action.user_id
        )
        if not member.request_until or member.request_until <= now():
            member.request_until = now() + group.settings["failure_ban_seconds"]
        verification.state, verification.updated_at = "failed", now()
        verification.failure_action = "decline"
        await cleanup(session, verification)

    async def apply_failure(self, session, action, group, verification, member):
        strategy = action.payload.get("applied_strategy") or group.settings.get(
            "verification_failure_action", "permanent_mute"
        )
        action.payload = {**action.payload, "applied_strategy": strategy}
        if strategy == "permanent_mute":
            await self.restrictions.verification_mute(session, action, group, verification, member)
            member.owned_restriction = {
                **member.owned_restriction,
                "kind": "verification_failed",
                "previous": verification.previous_permissions,
            }
        else:
            await self.restrictions.kick_member(session, group, member, action, "verification_kick")
        verification.state, verification.failure_action, verification.updated_at = (
            "failed",
            strategy,
            now(),
        )
        verification.error = action.reason
        await cleanup(session, verification)
