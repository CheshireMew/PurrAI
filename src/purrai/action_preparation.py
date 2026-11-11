"""Shared action identity checks and explicit domain handler registrations."""

from dataclasses import dataclass
from typing import Callable

from purrai.action_policy import administrator_protected_action
from purrai.deletion_effects import deletion_result
from purrai.member_decisions import current_decision
from purrai.models import Group, Member, Verification
from purrai.restriction_state import restriction_identity


@dataclass(frozen=True)
class ActionHandler:
    execute: Callable
    prepare: Callable
    check_member: bool | Callable = True


class ActionPreparation:
    def __init__(self, transport, policy):
        self.transport, self.policy = transport, policy

    async def load(self, session, action):
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
        if not await current_decision(session, action):
            action.state, action.error = "cancelled", "管理员已作出新的成员处理决定"
            return
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
        return group, verification, member

    async def authorize(self, session, action, check_member):
        await self.transport.authorize_actor(session, action)
        if check_member and administrator_protected_action(action):
            await self.transport.call(
                session, action, "get_chat_member", chat_id=action.group_id, user_id=action.user_id,
            )
