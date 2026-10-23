"""Durable action scheduling and dispatch to cohesive handlers."""

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import and_, select

from purrai.action_execution import ActionExecution, ActionSuperseded
from purrai.action_policy import NOTIFICATION_ACTIONS, ActionPolicy, administrator_protected_action
from purrai.action_transport import ActionTransport
from purrai.archive import deletion_result
from purrai.automatic_actions import AutomaticActions
from purrai.diagnostics import failure
from purrai.member_restrictions import MemberRestrictions
from purrai.models import (
    Action,
    Group,
    Member,
    Verification,
    now,
)
from purrai.notification_actions import NotificationActions
from purrai.restriction_actions import RestrictionActions
from purrai.restriction_state import owns_verification_ban, restriction_identity
from purrai.verification_actions import VerificationActions
from purrai.verification_state import ACTIVE
from purrai.work_claims import WorkClaims

logger = logging.getLogger(__name__)


class ActionRunner:
    def __init__(self, db, telegram, settings, verification, trust, progress, emoji_status, access):
        self.db, self.tg, self.settings = db, telegram, settings
        self.verification, self.progress, self.emoji_status = verification, progress, emoji_status
        self.action_claims, self.cleanup_lock = WorkClaims(), asyncio.Lock()
        self.policy = ActionPolicy(settings)
        transport = ActionTransport(telegram, self.policy, trust)
        self.action_call = transport.call
        restrictions = MemberRestrictions(transport, emoji_status)
        administration = RestrictionActions(transport, trust)
        verification_actions = VerificationActions(transport, verification, restrictions, administration)
        automatic = AutomaticActions(restrictions)
        self.notifications = NotificationActions(db, transport, access)
        self.handlers = {
            "verify_mute": verification_actions.mute,
            "challenge": verification_actions.challenge,
            "release": verification_actions.release,
            "ban": verification_actions.fail,
            "verification_failure": verification_actions.fail,
            "unmute": administration.restore,
            "manual_ban": administration.ban,
            "manual_mute": administration.mute,
            "unban": administration.unban,
            "contact_kick": automatic.contact_kick,
            "moderation_mute": automatic.legacy_mute,
            "emoji_status_mute": automatic.emoji_mute,
            "profile_mute": automatic.profile_mute,
            "rule_mute": automatic.rule_mute,
            "delete": self.notifications.delete,
            "admin_notify": self.notifications.notify,
            "join_link": self.notifications.join_link,
            "archive_export": self.notifications.export_archive,
            "warn": self.notifications.warn,
        }

    async def run_one_action(self, *, include_progress=True, include_cleanup=True, lane=None) -> bool:
        processed = await self._run_one_action(include_progress=include_progress, lane=lane)
        if processed or not include_cleanup:
            return processed
        return await self.run_one_cleanup()

    async def run_one_cleanup(self) -> bool:
        async with self.cleanup_lock:
            return await self._run_one_action(include_progress=False, cleanup_only=True)

    async def claim_action(self, *, include_progress, cleanup_only, lane):
        cleanup = and_(Action.kind == "delete", Action.dedupe_key.startswith("cleanup:"))
        query = select(Action).where(
            Action.state == "pending", Action.available_at <= now(), cleanup if cleanup_only else ~cleanup
        )
        if not include_progress:
            query = query.where(Action.kind.not_in(("progress", "emoji_status_check")))
        if lane == "member":
            query = query.where(Action.kind.not_in(NOTIFICATION_ACTIONS))
        elif lane == "notification":
            query = query.where(Action.kind.in_(NOTIFICATION_ACTIONS))
        async with self.action_claims.lock:
            async with self.db.sessions() as session:
                offset = 0
                while True:
                    rows = (
                        await session.scalars(
                            query.order_by(Action.priority, Action.created_at, Action.id)
                            .offset(offset)
                            .limit(100)
                        )
                    ).all()
                    if not rows:
                        return None
                    for action in rows:
                        if cleanup_only:
                            scope = ("cleanup", action.id)
                        elif action.kind in NOTIFICATION_ACTIONS:
                            scope = (
                                "notification",
                                action.group_id,
                                action.actor_id or action.payload.get("chat_id") or action.user_id,
                            )
                        elif action.user_id:
                            scope = ("member", action.group_id, action.user_id)
                        else:
                            scope = ("action", action.id)
                        if self.action_claims.claim(action.id, scope):
                            return action.id, action.kind, scope
                    offset += len(rows)

    async def _run_one_action(self, *, include_progress=True, cleanup_only=False, lane=None) -> bool:
        claimed = await self.claim_action(
            include_progress=include_progress, cleanup_only=cleanup_only, lane=lane
        )
        if claimed is None:
            return False
        identifier, kind, scope = claimed
        try:
            if kind == "emoji_status_check":
                return await self.emoji_status.run_one(identifier)
            if kind == "progress":
                return await self.progress.run_one(identifier)
            return await self.execute_claimed_action(identifier)
        finally:
            self.action_claims.release(identifier, scope)

    async def execute_claimed_action(self, identifier):
        async with ActionExecution(self.db) as session:
            action = await session.get(Action, identifier)
            if not action or action.state != "pending" or action.available_at > now():
                return False
            action.attempts += 1
            if action.kind == "challenge":
                started = now()
                action.payload = {
                    **action.payload,
                    "timing": {
                        "attempt_started_at": started,
                        "queue_seconds": round(max(0, started - action.available_at), 3),
                    },
                }
            try:
                finished = await self.execute(session, action)
                if action.state == "pending" and finished is not False:
                    action.state = "succeeded"
                if action.state == "succeeded":
                    action.error = ""
            except ActionSuperseded:
                if action.state == "pending":
                    action.state = "cancelled"
                    action.error = "成员批次或管理决定已变化，已停止旧任务"
            except TelegramRetryAfter as exc:
                failure(
                    logger,
                    self.settings,
                    exc,
                    "Telegram 任务限流",
                    f"action={action.id} kind={action.kind} group={action.group_id}",
                )
                action.available_at = now() + exc.retry_after
                action.error = f"Telegram 限流，{exc.retry_after} 秒后重试"
            except (TelegramForbiddenError, TelegramBadRequest, ValueError) as exc:
                action.state = "failed"
                action.error = failure(
                    logger,
                    self.settings,
                    exc,
                    "任务执行失败",
                    f"action={action.id} kind={action.kind} group={action.group_id}",
                    terminal=True,
                )
                group = await session.get(Group, action.group_id) if action.group_id else None
                if group and isinstance(exc, TelegramForbiddenError):
                    group.status = "error"
                await self.mark_error(session, action)
            except Exception as exc:
                action.available_at = now() + min(300, 2 ** min(action.attempts, 8))
                action.error = failure(
                    logger,
                    self.settings,
                    exc,
                    "任务执行重试",
                    f"action={action.id} kind={action.kind} group={action.group_id} attempt={action.attempts}",
                    terminal=action.attempts >= 8,
                )
                if action.attempts >= 8:
                    action.state = "failed"
                await self.mark_error(session, action)
            action.updated_at = now()
            await deletion_result(session, action)
            return True

    async def mark_error(self, session, action):
        if action.payload.get("verification_id"):
            verification = await session.get(Verification, action.payload["verification_id"])
            if verification:
                verification.error = action.error

    async def action_loop(self, lane=None):
        while True:
            try:
                if not self.tg.connected or not self.tg.user_id:
                    await asyncio.sleep(0.3)
                    continue
                if lane is None:
                    await self.verification.scan_timeouts()
                if not await self.run_one_action(include_progress=False, include_cleanup=False, lane=lane):
                    await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure(logger, self.settings, exc, "任务循环异常", terminal=True)
                await asyncio.sleep(2)

    async def prepare(self, session, action):
        group = await session.get(Group, action.group_id) if action.group_id else None
        if group and not group.approved and not action.dedupe_key.startswith("cleanup:"):
            action.state, action.error = "cancelled", "此群未获准接入"
            return
        verification = (
            await session.get(Verification, action.payload["verification_id"])
            if (action.payload.get("verification_id"))
            else None
        )
        member = await session.get(Member, (action.group_id, action.user_id)) if action.user_id else None
        if await self.policy.moderation_paused(session, action):
            action.state, action.error = "cancelled", "群保护已暂停，已停止自动处置"
            return
        if await self.policy.automatic_exemption(session, action):
            action.state = "cancelled"
            await deletion_result(session, action)
            return
        if action.payload.get("generation") and (
            not member or member.generation != action.payload["generation"]
        ):
            action.state = "cancelled"
            return
        if "restriction" in action.payload and restriction_identity(member) != action.payload["restriction"]:
            action.state, action.error = "cancelled", "原限制已被替换，已停止旧解除任务"
            return
        if action.kind == "unban" and action.payload.get("source_verification_id"):
            source = await session.get(Verification, action.payload["source_verification_id"])
            current_generation = bool(
                source and source.state == "failed" and member and member.generation == source.generation
            )
            owns_ban = current_generation and (
                source.flow == "join_request"
                and action.payload.get("request_cooldown")
                or await owns_verification_ban(session, member, source)
            )
            if not owns_ban:
                action.state, action.error = "cancelled", "该验证对应的入群批次或封禁已变化，已停止旧解除任务"
                return
        if action.kind in ("unmute", "unban") and action.payload.get("violation_id"):
            owned = member.owned_restriction if member else None
            source = (
                await session.get(Action, owned["source_action"])
                if owned and owned.get("source_action")
                else None
            )
            if not source or source.payload.get("violation_id") != action.payload["violation_id"]:
                action.state, action.error = "cancelled", "该违规对应的限制已被替换，已停止旧解除任务"
                return
        if verification and (
            verification.state not in ACTIVE or not member or member.generation != verification.generation
        ):
            action.state = "cancelled"
            return
        if verification and action.payload.get("expected_nonce") not in (None, verification.nonce):
            action.state = "cancelled"
            return
        if (
            verification
            and action.kind in ("verify_mute", "challenge", "progress")
            and (verification.state not in ("muting", "pending"))
        ):
            action.state = "cancelled"
            return
        if verification and (
            action.kind == "release"
            and verification.state != "releasing"
            or action.kind == "ban"
            and verification.state != "removing"
            or action.kind == "verification_failure"
            and verification.state != "failing"
        ):
            action.state = "cancelled"
            return
        if verification and action.kind in ("verify_mute", "challenge") and verification.deadline <= now():
            await self.verification.fail_verification(session, verification, "验证超时")
            action.state = "cancelled"
            return
        # These handlers perform their own checked member lookup before any
        # mutation. Repeating it here adds a Telegram round trip to every join.
        if administrator_protected_action(action) and action.kind not in (
            "verify_mute", "verification_failure", "release",
        ) and not (
            verification and verification.flow == "join_request" and member and not member.present
        ):
            await self.action_call(
                session, action, "get_chat_member", chat_id=action.group_id, user_id=action.user_id
            )
        return group, verification, member

    async def execute(self, session, action):
        if action.kind == "delete" and action.dedupe_key.startswith("cleanup:"):
            return await self.notifications.delete_action_message(session, action)
        prepared = await self.prepare(session, action)
        if prepared is None:
            return
        handler = self.handlers.get(action.kind)
        if handler is None:
            raise ValueError(f"未知任务类型：{action.kind}")
        return await handler(session, action, *prepared)
