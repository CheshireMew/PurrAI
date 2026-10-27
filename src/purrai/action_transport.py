"""Checked network handoffs for durable actions."""

from purrai.action_execution import ActionPreflightError, ActionSuperseded
from purrai.action_policy import administrator_protected_action
from purrai.action_queue import enqueue
from purrai.errors import ManagementError
from purrai.member_decisions import current_decision
from purrai.telegram import is_admin


class ActionTransport:
    def __init__(self, telegram, policy, trust, access):
        self.tg, self.policy, self.trust, self.access = telegram, policy, trust, access

    async def authorize_actor(self, session, action):
        if not await current_decision(session, action):
            raise ActionSuperseded(reason="管理员已作出新的成员处理决定", before_request=True)
        if action.actor_id is None or action.group_id is None:
            return
        try:
            await session.info["action_execution"].external(
                lambda: self.access.authorize(action.actor_id, action.group_id, write=True)
            )
        except ActionSuperseded as exc:
            raise ActionSuperseded(reason=str(exc), before_request=True) from exc
        except ManagementError as exc:
            if exc.status in (403, 404):
                raise ActionSuperseded(reason=f"操作者当前已无此群管理权限：{exc}", before_request=True) from exc
            # Temporary inability to check a role is retryable, without making
            # any Telegram mutation or losing the accepted administrative task.
            raise ActionPreflightError(str(exc)) from exc

    async def call(self, session, action, method, *, before_send=None, **kwargs):
        execution = session.info["action_execution"]
        if not method.startswith("get_"):
            await self.authorize_actor(session, action)
        if await self.policy.moderation_paused(session, action):
            raise ActionSuperseded(before_request=True)
        try:
            if before_send is not None:
                before_send()
            result = await execution.external(lambda: self.tg.call(method, **kwargs))
            # Read-only lookups are decision boundaries. A pause that happened
            # during a lookup must prevent the following punishment request.
            # Once Telegram accepted a mutation, finish recording its result
            # (including the unban half of a kick) instead of losing that fact.
            if method.startswith("get_") and await self.policy.moderation_paused(session, action):
                raise ActionSuperseded(result)
            if await self.policy.automatic_exemption(session, action):
                raise ActionSuperseded(result)
            if (
                administrator_protected_action(action)
                and method == "get_chat_member"
                and kwargs.get("chat_id") == action.group_id
                and kwargs.get("user_id") == action.user_id
                and is_admin(result)
            ):
                await self.trust.exempt_administrator(session, action.group_id, action.user_id)
                raise ActionSuperseded(result)
            return result
        except ActionSuperseded as exc:
            # A sent challenge can finish after an administrator replaced it.
            # Remove that exact stale prompt without writing into the new generation.
            if method == "send_photo" and isinstance(exc.result, dict) and exc.result.get("message_id"):
                await enqueue(
                    session,
                    "delete",
                    f"cleanup:late:{action.id}:{exc.result['message_id']}",
                    action.group_id,
                    action.user_id,
                    {"chat_id": kwargs["chat_id"], "message_id": exc.result["message_id"]},
                    5,
                )
            raise
