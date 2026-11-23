"""Apply confirmed administrator roles and explicit administrator trust."""

import secrets

from sqlalchemy import select

from purrai.action_policy import (
    AUTOMATIC_MEMBER_ACTIONS,
    TRUST_CANCELLABLE_ACTIONS,
    administrator_protected_action,
)
from purrai.action_queue import audit
from purrai.deletion_tracking import track_deletion
from purrai.models import (
    Action,
    Member,
    Verification,
    Whitelist,
    now,
)
from purrai.verification_cleanup import cleanup
from purrai.verification_state import ACTIVE


class MemberTrust:
    def __init__(self, settings):
        self.settings = settings

    async def exempt_administrator(self, session, group_id, user_id):
        """Persist role-derived trust while retaining any independent manual whitelist."""
        white = await session.get(Whitelist, (group_id, user_id))
        if white is None:
            white = Whitelist(group_id=group_id, user_id=user_id,
                administrator_backup={"existed": False})
            session.add(white)
        elif white.administrator_backup is None:
            white.administrator_backup = {"existed": True, "verification": white.verification,
                "moderation": white.moderation, "note": white.note}
        white.verification, white.moderation = True, True
        if not white.note:
            white.note = "Telegram 群主或管理员自动加入白名单；撤职时仅移除身份带来的豁免"
        for action in await session.scalars(
            select(Action).where(
                Action.group_id == group_id,
                Action.user_id == user_id,
                Action.state == "pending",
                Action.actor_id.is_(None),
                Action.kind.in_(AUTOMATIC_MEMBER_ACTIONS),
            )
        ):
            if administrator_protected_action(action):
                action.state, action.error, action.updated_at = "cancelled", "群主或管理员免自动限制", now()
                await track_deletion(session, action)
        for verification in await session.scalars(
            select(Verification).where(
                Verification.group_id == group_id,
                Verification.user_id == user_id,
                Verification.state.in_(ACTIVE),
            )
        ):
            verification.state, verification.error, verification.failure_action = "passed", "", ""
            verification.nonce, verification.updated_at = secrets.token_hex(4), now()
            await cleanup(session, verification)
        member = await session.get(Member, (group_id, user_id))
        if member:
            member.passed_at, member.request_until, member.present = now(), 0, True
            member.owned_restriction, member.restriction_conflict = None, False

    async def revoke_administrator(self, session, group_id, user_id):
        white = await session.get(Whitelist, (group_id, user_id))
        if white is None or white.administrator_backup is None:
            return
        previous = white.administrator_backup
        if not previous["existed"]:
            await session.delete(white)
        else:
            white.verification, white.moderation, white.note = (
                previous["verification"], previous["moderation"], previous["note"])
            white.administrator_backup = None

    async def trust_member(self, session, member, actor_id, reason, exclude_action=None):
        white = await session.get(Whitelist, (member.group_id, member.user_id))
        if white is None:
            white = Whitelist(group_id=member.group_id, user_id=member.user_id)
            session.add(white)
        white.verification, white.moderation = True, True
        white.administrator_backup = None
        white.note = f"管理员 {actor_id} 手动解禁后自动加入白名单：{reason}"[:300]
        for pending in await session.scalars(
            select(Action).where(
                Action.group_id == member.group_id,
                Action.user_id == member.user_id,
                Action.state == "pending",
                Action.actor_id.is_(None),
                Action.kind.in_(TRUST_CANCELLABLE_ACTIONS),
            )
        ):
            if pending.id != exclude_action and not pending.dedupe_key.startswith("cleanup:"):
                pending.state, pending.error, pending.updated_at = (
                    "cancelled",
                    "管理员解禁，已加入本群白名单",
                    now(),
                )
                await track_deletion(session, pending)
        for verification in await session.scalars(
            select(Verification).where(
                Verification.group_id == member.group_id,
                Verification.user_id == member.user_id,
                Verification.generation == member.generation,
                Verification.state.in_(ACTIVE),
            )
        ):
            verification.state, verification.error, verification.updated_at = "passed", "", now()
            verification.nonce = secrets.token_hex(4)
            await cleanup(session, verification)
        member.passed_at = now()
        member.request_until = 0
        await audit(
            session,
            "whitelist",
            actor_id,
            member.group_id,
            reason,
            {"user_id": member.user_id, "verification": True, "moderation": True},
        )
