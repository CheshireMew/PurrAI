"""Durable action scheduling and dispatch to cohesive handlers."""

import asyncio
import json
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramNetworkError
from sqlalchemy import and_, func, literal_column, or_, select, tuple_
from sqlalchemy.orm import aliased

from purrai.action_execution import ActionExecution, ActionSuperseded, ChallengeDeliveryRetry, MemberStateChanged, RestrictionPreserved
from purrai.action_policy import NOTIFICATION_ACTIONS, ActionPolicy
from purrai.action_preparation import ActionHandler, ActionPreparation
from purrai.action_transport import ActionTransport
from purrai.deletion_effects import deletion_result
from purrai.automatic_actions import AutomaticActions
from purrai.channel_moderation import ChannelActions
from purrai.channel_pins import ChannelPinActions
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
    def __init__(self, db, telegram, settings, verification, trust, progress, emoji_status, access, advertising, identity_protection):
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
        channels = ChannelActions(transport)
        channel_pins = ChannelPinActions(transport)
        async def identity_check(session, action, group, verification, member):
            return await identity_protection.execute(session, action, group, verification, member, restrictions)
        self.notifications = NotificationActions(db, transport)
        self.preparation = ActionPreparation(transport, self.policy)
        self.report_error = verification_actions.record_error
        self.preserve_restriction = verification_actions.preserve_restriction
        self.handlers = {
            "linked_channel_unpin": ActionHandler(channel_pins.unpin, channel_pins.prepare, False),
            "channel_ad_ban": ActionHandler(channels.ban, channels.prepare, False),
            "channel_unban": ActionHandler(channels.unban, channels.prepare, False),
            "identity_check": ActionHandler(identity_check, automatic.prepare, False),
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
            "sample_ad_mute": ActionHandler(automatic.sample_ad_mute, automatic.prepare),
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
            "points_message": ActionHandler(self.notifications.points_message, self.notifications.prepare, False),
            "lottery_notice": ActionHandler(self.notifications.lottery_notice, self.notifications.prepare, False),
            "group_menu": ActionHandler(self.notifications.group_menu, self.notifications.prepare, False),
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
        def conditions(model):
            cleanup = and_(model.kind == "delete", model.dedupe_key.startswith("cleanup:"))
            result = [model.state == "pending", cleanup if cleanup_only else ~cleanup]
            if not include_progress:
                result.append(model.kind.not_in(("progress", "emoji_status_check", "advertisement_check")))
            if lane == "member":
                result.append(model.kind.not_in(NOTIFICATION_ACTIONS))
            elif lane == "notification":
                result.append(model.kind.in_(NOTIFICATION_ACTIONS))
            return result

        async with self.action_claims.selecting():
            async with self.db.sessions() as session:
                stamp = now()
                order = (Action.priority, Action.created_at, literal_column("actions.rowid"))
                query = select(Action).where(*conditions(Action), Action.available_at <= stamp)
                if self.action_claims.identifiers:
                    query = query.where(Action.id.not_in(self.action_claims.identifiers))
                if not cleanup_only:
                    if self.action_claims.scopes:
                        scopes = [json.dumps(scope, separators=(",", ":"), ensure_ascii=False)
                                  for scope in self.action_claims.scopes]
                        query = query.where(Action.queue_scope.not_in(scopes))
                    earlier = aliased(Action, name="earlier_action")
                    predecessor = select(earlier.id).where(
                        *conditions(earlier), earlier.queue_scope == Action.queue_scope,
                        tuple_(earlier.priority, earlier.created_at, literal_column("earlier_action.rowid")) < tuple_(*order),
                        # Future automatic restores do not reserve a member's stream.
                        or_(earlier.available_at <= stamp,
                            ~func.coalesce(earlier.payload["automatic_restore"].as_boolean(), False)),
                    ).correlate(Action).exists()
                    query = query.where(~predecessor)
                action = await session.scalar(query.order_by(*order).limit(1))
                if action is None:
                    return None
                scope = ("cleanup", action.id) if cleanup_only else tuple(json.loads(action.queue_scope))
                if self.action_claims.claim(action.id, scope):
                    return action.id, action.kind, scope
                return None

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
        from time import perf_counter
        started = perf_counter()
        async with ActionExecution(self.db) as session:
            session.info["telegram"] = self.tg
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
                if action.kind == "delete" and action.state == "pending" and isinstance(exc, MemberStateChanged):
                    # Re-enter normal preparation with current permissions, message evidence and exemptions.
                    action.available_at = now() + min(30, 2 ** min(action.attempts, 5))
                    action.error = "成员状态已变化，将重新核对消息删除条件"
                elif action.state == "pending":
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
                recovering = self.recovery_retry(action, exc)
                action.state = "pending" if recovering else "failed"
                if recovering:
                    action.available_at = now() + min(300, 2 ** min(action.attempts, 8))
                action.error = failure(
                    logger,
                    self.settings,
                    exc,
                    "任务执行失败",
                    f"action={action.id} kind={action.kind} group={action.group_id}",
                    terminal=not recovering,
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
            if action.kind in ("warn", "admin_notify", "group_menu"):
                # A send can be confirmed even if a concurrent member update cancelled the action.
                from purrai.notice_cleanup import schedule_notice_cleanup
                await schedule_notice_cleanup(session, action)
            await deletion_result(session, action)
            from purrai.message_events import queue_related_delete
            await queue_related_delete(session, action)
            logger.info("操作处理结果 action=%s kind=%s group=%s state=%s attempt=%s elapsed=%.3fs",
                        action.id, action.kind, action.group_id, action.state, action.attempts, perf_counter() - started)
            return True

    @staticmethod
    def recovery_retry(action, exc):
        if action.kind == "delete" and action.dedupe_key.startswith(("cleanup:command:", "cleanup:notice:")):
            return (now() - action.created_at < 47 * 3600 and isinstance(exc,
                (TelegramNetworkError, TimeoutError, ConnectionError, OSError, TelegramForbiddenError, TelegramBadRequest)))
        return (action.kind in ("release", "subscription_check") or action.payload.get("automatic_restore")) and isinstance(
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
