"""Checked network handoffs for durable actions."""

from purrai.action_execution import ActionSuperseded
from purrai.action_policy import administrator_protected_action
from purrai.action_queue import enqueue
from purrai.telegram import is_admin


class ActionTransport:
    def __init__(self, telegram, policy, trust):
        self.tg, self.policy, self.trust = telegram, policy, trust

    async def call(self, session, action, method, **kwargs):
        execution = session.info["action_execution"]
        if await self.policy.moderation_paused(session, action):
            raise ActionSuperseded()
        try:
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
