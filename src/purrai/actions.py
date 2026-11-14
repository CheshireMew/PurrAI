"""Durable action scheduling and dispatch to cohesive handlers."""

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramNetworkError
from sqlalchemy import and_, literal_column, select

from purrai.action_execution import ActionExecution, ActionSuperseded, ChallengeDeliveryRetry, RestrictionPreserved
from purrai.action_policy import NOTIFICATION_ACTIONS, ActionPolicy
from purrai.action_preparation import ActionHandler, ActionPreparation
from purrai.action_transport import ActionTransport
from purrai.deletion_effects import deletion_result
from purrai.automatic_actions import AutomaticActions
from purrai.diagnostics import failure
from purrai.member_restrictions import MemberRestrictions
from purrai.models import (
    Action,
    Group,
    now,
)
from purrai.notification_actions import NotificationActions
from purrai.restriction_actions import RestrictionActions
from purrai.verification_actions import VerificationActions
from purrai.work_claims import WorkClaims

logger = logging.getLogger(__name__)


class ActionRunner:
    def __init__(self, db, telegram, settings, verification, trust, progress, emoji_status, access, advertising):
        self.db, self.tg, self.settings = db, telegram, settings
        self.verification, self.progress, self.emoji_status = verification, progress, emoji_status
        self.advertising = advertising
        self.action_claims, self.cleanup_lock = WorkClaims(), asyncio.Lock()
        self.policy = ActionPolicy(settings)
        transport = ActionTransport(telegram, self.policy, trust, access)
        self.transport = transport
        self.action_call = transport.call
        restrictions = MemberRestrictions(transport, emoji_status)
        administration = RestrictionActions(transport, trust)
        verification_actions = VerificationActions(transport, verification, restrictions, administration)
        automatic = AutomaticActions(restrictions)
        self.notifications = NotificationActions(db, transport)
        self.preparation = ActionPreparation(transport, self.policy)
        self.report_error = verification_actions.record_error
        self.preserve_restriction = verification_actions.preserve_restriction
        self.handlers = {
            "verify_mute": ActionHandler(verification_actions.mute, verification_actions.prepare, verification_actions.requires_member_check),
            "challenge": ActionHandler(verification_actions.challenge, verification_actions.prepare, verification_actions.requires_member_check),
            "subscription_check": ActionHandler(verification_actions.subscription_check, verification_actions.prepare, verification_actions.requires_member_check),
            "release": ActionHandler(verification_actions.release, verification_actions.prepare, verification_actions.requires_member_check),
            "ban": ActionHandler(verification_actions.fail, verification_actions.prepare, verification_actions.requires_member_check),
            "verification_failure": ActionHandler(verification_actions.fail, verification_actions.prepare, verification_actions.requires_member_check),
            "unmute": ActionHandler(administration.restore, administration.prepare),
            "manual_ban": ActionHandler(administration.ban, administration.prepare),
            "manual_mute": ActionHandler(administration.mute, administration.prepare),
            "spam_mute": ActionHandler(administration.spam_mute, administration.prepare),
            "ai_ad_mute": ActionHandler(automatic.ai_ad_mute, automatic.prepare),
            "unban": ActionHandler(administration.unban, administration.prepare),
            "contact_kick": ActionHandler(automatic.contact_kick, automatic.prepare),
            "contact_ban": ActionHandler(automatic.contact_ban, automatic.prepare),
            "advertisement_ban": ActionHandler(automatic.advertisement_ban, automatic.prepare),
            "moderation_mute": ActionHandler(automatic.legacy_mute, automatic.prepare),
            "emoji_status_mute": ActionHandler(automatic.emoji_mute, automatic.prepare),
            "emoji_status_ban": ActionHandler(automatic.emoji_ban, automatic.prepare),
            "profile_mute": ActionHandler(automatic.profile_mute, automatic.prepare),
            "rule_mute": ActionHandler(automatic.rule_mute, automatic.prepare),
            "delete": ActionHandler(self.notifications.delete, self.notifications.prepare),
            "admin_notify": ActionHandler(self.notifications.notify, self.notifications.prepare),
            "join_link": ActionHandler(self.notifications.join_link, self.notifications.prepare),
            "archive_export": ActionHandler(self.notifications.export_archive, self.notifications.prepare),
            "warn": ActionHandler(self.notifications.warn, self.notifications.prepare),
            "auto_reply": ActionHandler(self.notifications.auto_reply, self.notifications.prepare, False),
            "owner_alert": ActionHandler(self.notifications.owner_alert, self.notifications.prepare, False),
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
            Action.state == "pending", cleanup if cleanup_only else ~cleanup
        )
        if not include_progress:
            query = query.where(Action.kind.not_in(("progress", "emoji_status_check", "advertisement_check")))
        if lane == "member":
            query = query.where(Action.kind.not_in(NOTIFICATION_ACTIONS))
        elif lane == "notification":
            query = query.where(Action.kind.in_(NOTIFICATION_ACTIONS))
        async with self.action_claims.lock:
            async with self.db.sessions() as session:
                offset, blocked = 0, set()
                while True:
                    rows = (
                        await session.scalars(
                            query.order_by(Action.priority, Action.created_at, literal_column("actions.rowid"))
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
                        if scope in blocked:
                            continue
                        blocked.add(scope)
                        if action.available_at > now():
                            continue
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
            if kind == "advertisement_check":
                return await self.advertising.run_one(identifier)
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
            try:
                finished = await self.execute(session, action)
                if action.state == "pending" and finished is not False:
                    action.state = "succeeded"
                if action.state == "succeeded":
                    action.error = ""
            except ActionSuperseded as exc:
                if action.state == "pending":
                    action.state = "cancelled"
                    action.error = str(exc)
                if isinstance(exc, RestrictionPreserved):
                    action.payload = {**action.payload, "outcome": "external_restriction_preserved"}
                    await self.preserve_restriction(session, action)
            except ChallengeDeliveryRetry as exc:
                action.available_at = now() + min(300, 2 ** min(action.attempts, 9))
                action.error = str(exc)
                await self.report_error(session, action)
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
                await self.report_error(session, action)
            except Exception as exc:
                action.available_at = now() + min(300, 2 ** min(action.attempts, 8))
                action.error = failure(
                    logger,
                    self.settings,
                    exc,
                    "任务执行重试",
                    f"action={action.id} kind={action.kind} group={action.group_id} attempt={action.attempts}",
                    terminal=action.attempts >= 8 and not self.recovery_retry(action, exc),
                )
                if action.attempts >= 8 and not self.recovery_retry(action, exc):
                    action.state = "failed"
                await self.report_error(session, action)
            action.updated_at = now()
            await deletion_result(session, action)
            return True

    @staticmethod
    def recovery_retry(action, exc):
        return action.kind in ("release", "subscription_check") and isinstance(
            exc, (TelegramNetworkError, TimeoutError, ConnectionError, OSError))

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

    async def execute(self, session, action):
        if action.kind == "delete" and action.dedupe_key.startswith("cleanup:"):
            return await self.notifications.delete_action_message(session, action)
        handler = self.handlers.get(action.kind)
        if handler is None:
            raise ValueError(f"未知任务类型：{action.kind}")
        prepared = await self.preparation.load(session, action)
        if prepared is None or not await handler.prepare(session, action, *prepared):
            return
        check_member = handler.check_member(action, *prepared) if callable(handler.check_member) else handler.check_member
        await self.preparation.authorize(session, action, check_member)
        return await handler.execute(session, action, *prepared)
