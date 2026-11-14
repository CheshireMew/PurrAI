"""Telegram effects of verification decisions."""

import base64
import logging
import secrets
from time import perf_counter

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from purrai import captcha
from purrai.action_queue import enqueue
from purrai.action_execution import ActionPreflightError, ChallengeDeliveryRetry
from purrai.action_delivery import deliver_once, uncertain_delivery
from purrai.models import (
    Verification,
    now,
)
from purrai.telegram import is_present
from purrai.verification_cleanup import cleanup
from purrai.verification_state import matches_action, confirm_prompt, ACTIVE

logger = logging.getLogger(__name__)


class VerificationActions:
    def __init__(self, transport, verification, restrictions, administration):
        self.action_call, self.verification = transport.call, verification
        self.restrictions, self.administration = restrictions, administration

    async def prepare(self, session, action, group, verification, member):
        if action.kind == "challenge":
            started = now()
            action.payload = {**action.payload, "timing": {
                "attempt_started_at": started,
                "queue_seconds": round(max(0, started - action.available_at), 3),
            }}
        if not matches_action(action, verification, member):
            action.state = "cancelled"
            return False
        expected = {
            "verify_mute": ("muting", "pending"), "challenge": ("muting", "pending"),
            "release": ("releasing",), "ban": ("removing",), "verification_failure": ("failing",),
        }
        if verification and verification.state not in expected[action.kind]:
            action.state = "cancelled"
            return False
        if verification and action.kind in ("verify_mute", "challenge") and verification.message_id and verification.deadline <= now():
            await self.verification.fail_verification(session, verification, "验证超时")
            action.state = "cancelled"
            return False
        if (verification and action.kind == "challenge" and not verification.message_id
                and verification.flow == "join_request" and verification.request_date + 240 <= now()):
            raise ValueError("入群申请私聊窗口已过期，请重新申请；未确认验证码送达，不执行验证处罚")
        if verification and action.kind == "verification_failure" and not verification.message_id:
            verification.state, verification.failure_action = "muting", ""
            verification.error = "验证码尚未确认送达，取消超时处罚并重新发送"
            action.state, action.error = "cancelled", verification.error
            await enqueue(session, "challenge", f"challenge:{verification.id}:{verification.nonce}",
                          group.id, member.user_id, {"verification_id": verification.id}, 1)
            return False
        return True

    @staticmethod
    def requires_member_check(action, group, verification, member):
        # These handlers check the member themselves. Join requests may refer
        # to someone who has not entered the group yet.
        return action.kind not in ("verify_mute", "verification_failure", "release") and not (
            verification and verification.flow == "join_request" and member and not member.present
        )

    @staticmethod
    async def record_error(session, action):
        if action.payload.get("verification_id"):
            verification = await session.get(Verification, action.payload["verification_id"])
            if verification:
                verification.error = action.error

    @staticmethod
    async def preserve_restriction(session, action):
        verification = await session.get(Verification, action.payload.get("verification_id")) if action.payload.get("verification_id") else None
        if (verification and verification.state in ACTIVE
                and action.payload.get("expected_nonce") in (None, verification.nonce)):
            verification.state, verification.error = "superseded", action.error
            await cleanup(session, verification)

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
        await cleanup(session, verification)
        prepared = now()
        generation_started = perf_counter()
        saved_image = action.payload.get("challenge_image")
        if saved_image and action.payload.get("challenge_nonce") == verification.nonce:
            image = base64.b64decode(saved_image)
        else:
            legacy_retry = action.payload.get("challenge_delivery") in ("sending", "uncertain")
            code = next((value for value in verification.choices
                         if captcha.digest(value, verification.nonce) == verification.answer_hash), None) if legacy_retry else None
            if code:
                image = captcha.render_image(code)
            else:
                if legacy_retry:
                    verification.nonce = secrets.token_hex(4)
                    action.payload = {**action.payload, "expected_nonce": verification.nonce}
                code, image = captcha.make_image()
                verification.answer_hash = captcha.digest(code, verification.nonce)
                verification.input_digits = ""
                verification.choices = captcha.make_choices(code)
        verification.message_id = None
        verification.state = "pending"
        # Persist the exact image and answer before I/O so retries/restarts keep
        # the same buttons valid, including a prompt whose receipt was lost.
        started = now()
        attempt = action.payload.get("timing", {})
        timing = {
            **attempt,
            "preparation_seconds": round(max(0, prepared - attempt.get("attempt_started_at", prepared)), 3),
            "image_seconds": round(perf_counter() - generation_started, 3),
            "send_started_at": started,
        }
        action.payload = {**action.payload, "challenge_delivery": "preparing",
                          "challenge_nonce": verification.nonce, "timing": timing,
                          "challenge_image": base64.b64encode(image).decode("ascii")}
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
                    timeout_seconds=group.settings["verification_timeout"],
                ),
                reply_markup=captcha.keyboard(verification.id, verification.nonce, verification.choices),
            )
        except (TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError):
            self.delivery_result(action, timing, send_started, "rejected")
            raise
        except (TelegramNetworkError, TimeoutError, ConnectionError, OSError) as exc:
            verification.state = "pending"
            self.delivery_result(action, timing, send_started, "rejected" if isinstance(exc, ActionPreflightError) else "uncertain")
            raise ChallengeDeliveryRetry("验证码尚未确认送达，将自动重试；送达前不计算答题超时") from exc
        self.delivery_result(action, timing, send_started, "confirmed")
        action.payload = {key: value for key, value in action.payload.items() if key != "challenge_image"}
        confirm_prompt(verification, group, response["message_id"])

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
        async def call(session, action, method, *, before_send, **kwargs):
            def sending():
                until = action.payload.get("decline_until") or max(
                    member.request_until or 0, now() + group.settings["failure_ban_seconds"])
                action.payload = {**action.payload, "decline_until": until}
                member.request_until = until
                before_send()
            return await self.action_call(session, action, method, before_send=sending, **kwargs)

        try:
            await deliver_once(call, session, action, "decline_chat_join_request",
                               chat_id=group.id, user_id=action.user_id)
        except (TelegramBadRequest, TelegramForbiddenError, ValueError) as exc:
            # The request may already be gone, but absence cannot prove who
            # handled it. End this local generation with an explicit outcome.
            verification.state, verification.updated_at = "failed", now()
            verification.failure_action = "decline_uncertain" if uncertain_delivery(action.payload) else "decline_failed"
            verification.error = str(exc)
            action.state, action.error = "failed", str(exc)
            await cleanup(session, verification)
            return
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
