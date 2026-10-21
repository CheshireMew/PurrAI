"""Explicit administrator restrictions, restoration and ownership checks."""

from purrai.action_queue import enqueue
from purrai.models import (
    Member,
    now,
)
from purrai.restriction_state import restriction_matches, permission_state_matches, ban_matches
from purrai.telegram import ALL_PERMISSIONS, is_admin, is_present, muted_permissions
from purrai.verification_cleanup import cleanup


class RestrictionActions:
    def __init__(self, transport, trust):
        self.action_call, self.trust = transport.call, trust

    async def restore(self, session, action, group, verification, member):
        if (
            action.kind == "unmute"
            and member
            and member.owned_restriction
            and member.owned_restriction.get("kind") == "verification"
        ):
            raise ValueError("成员正在验证，请使用人工放行完成验证")
        if not member or not member.owned_restriction:
            if verification and member:
                current = await self.action_call(
                    session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id
                )
                if current.get("status") == "member" and not member.restriction_conflict:
                    verification.state, member.passed_at = "passed", now()
                    await cleanup(session, verification)
                    if action.actor_id:
                        await self.trust.trust_member(
                            session,
                            member,
                            action.actor_id,
                            action.reason or "管理员人工放行",
                            exclude_action=action.id,
                        )
                    return
            raise ValueError("没有可解除的 PurrAI 限制，请核实成员当前状态")
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id
        )
        owned = member.owned_restriction
        if member.restriction_conflict or not is_present(current) or is_admin(current):
            raise ValueError("成员权限已被其他管理员修改，需要人工处理")
        already_released = bool(
            owned.get("release_permissions") is not None
            and (
                current.get("status") == "member"
                or (
                    permission_state_matches(
                        current, owned["release_permissions"], owned.get("release_until", 0)
                    )
                )
            )
        )
        if not already_released and (
            current.get("status") == "restricted" and (not restriction_matches(current, owned))
        ):
            action.state = "failed"
            action.error = "成员权限已被其他管理员修改，需要人工处理"
            if verification:
                verification.error = action.error
            return
        chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
        defaults = chat.get("permissions", {})
        previous = verification.previous_permissions if verification else owned.get("previous", {})
        previous_until = previous.get("__until_date", 0)
        if previous_until and previous_until <= now():
            previous, previous_until = {}, 0
        restore = {key: bool(defaults.get(key, False) and previous.get(key, True)) for key in ALL_PERMISSIONS}
        if not already_released:
            member.owned_restriction = {
                **owned,
                "release_permissions": restore,
                "release_until": previous_until,
            }
            await self.action_call(
                session,
                action,
                "restrict_chat_member",
                chat_id=group.id,
                user_id=member.user_id,
                permissions=restore,
                use_independent_chat_permissions=True,
                until_date=previous_until,
            )
        member.owned_restriction = None
        if verification:
            verification.state = "passed"
            verification.failure_action = ""
            verification.error = ""
            verification.updated_at = now()
            member.passed_at = now()
            await cleanup(session, verification)
        if action.actor_id:
            await self.trust.trust_member(
                session,
                member,
                action.actor_id,
                action.reason or "管理员手动解除禁言",
                exclude_action=action.id,
            )

    async def ban(self, session, action, group, verification, member):
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id
        )
        if is_admin(current) or member and member.restriction_conflict:
            raise ValueError("成员身份或权限已变化，需要人工处理")
        owned = member.owned_restriction if member else None
        reconciled = bool(
            owned and owned.get("source_action") == action.id and ban_matches(current, owned["until_date"])
        )
        if not reconciled:
            if current.get("status") == "kicked":
                raise ValueError("成员已有其他封禁，需要人工处理")
            until = (
                owned["until_date"]
                if owned and owned.get("source_action") == action.id
                else int(now() + group.settings["failure_ban_seconds"])
            )
            if owned and owned.get("source_action") == action.id and until <= now():
                member.owned_restriction = None
                action.payload = {**action.payload, "outcome": "restriction_expired"}
                return
            if member:
                member.owned_restriction = {
                    "kind": "ban",
                    "source_action": action.id,
                    "until_date": until,
                    "verification_id": verification.id if verification else None,
                }
            await self.action_call(
                session, action, "ban_chat_member", chat_id=group.id, user_id=action.user_id, until_date=until
            )
        if verification:
            verification.state = "failed"
            verification.failure_action = "legacy_ban"
            verification.updated_at = now()
            await cleanup(session, verification)

    async def mute(self, session, action, group, verification, member):
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id
        )
        if not is_present(current) or is_admin(current):
            action.state = "cancelled"
            return
        if member and member.owned_restriction and member.owned_restriction.get("kind") == "verification":
            action.state = "cancelled"
            return
        if member and (
            member.restriction_conflict
            or action.payload.get("generation")
            and member.generation != action.payload["generation"]
        ):
            raise ValueError("成员权限或入群批次已变化，需要人工处理")
        if current.get("status") == "restricted" and not (member and member.owned_restriction):
            raise ValueError("保留管理员已有禁言，需要人工处理")
        if (
            current.get("status") == "restricted"
            and member
            and member.owned_restriction
            and (not restriction_matches(current, member.owned_restriction))
        ):
            raise ValueError("成员权限已经改变，需要人工处理")
        if member is None:
            member = Member(group_id=group.id, user_id=action.user_id, joined_at=now())
            session.add(member)
        owned = member.owned_restriction or {}
        reconciled = bool(
            owned.get("source_action") == action.id
            and current.get("status") == "restricted"
            and restriction_matches(current, owned)
        )
        if (
            owned.get("source_action") == action.id
            and owned["until_date"] <= now()
            and current.get("status") == "member"
        ):
            member.owned_restriction = None
            action.payload = {**action.payload, "outcome": "restriction_expired"}
            return
        if not reconciled:
            chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
            until = (
                owned.get("until_date")
                if owned.get("source_action") == action.id
                else int(now() + group.settings["mute_seconds"])
            )
            target = (
                owned.get("permissions")
                if owned.get("source_action") == action.id
                else muted_permissions(chat.get("permissions", {}))
            )
            member.owned_restriction = {
                "kind": "moderation",
                "source_action": action.id,
                "permissions": target,
                "until_date": until,
                "previous": {},
            }
            await self.action_call(
                session,
                action,
                "restrict_chat_member",
                chat_id=group.id,
                user_id=action.user_id,
                permissions=target,
                use_independent_chat_permissions=True,
                until_date=until,
            )
        await enqueue(
            session,
            "warn",
            f"mute-notice:{action.id}",
            group.id,
            action.user_id,
            {
                "text": f"用户 {action.user_id}：{'管理员处置' if action.kind == 'manual_mute' else '累计违规'}，已临时禁言 {group.settings['mute_seconds'] // 60} 分钟。"
            },
        )

    async def unban(self, session, action, group, verification, member):
        if action.payload.get("request_cooldown"):
            if member:
                member.request_until = 0
            return
        await self.unban_member(session, action, group, verification, member)

    async def unban_member(self, session, action, group, verification, member):
        if not member or not member.owned_restriction or member.owned_restriction.get("kind") != "ban":
            raise ValueError("没有 PurrAI 施加的封禁记录")
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id
        )
        if (
            member.owned_restriction.get("unban_action") == action.id
            and current.get("status") == "left"
            and not member.restriction_conflict
        ):
            member.owned_restriction = None
            return
        if member.restriction_conflict or not ban_matches(current, member.owned_restriction["until_date"]):
            raise ValueError("封禁状态已改变，需要人工处理")
        member.owned_restriction = {**member.owned_restriction, "unban_action": action.id}
        await self.action_call(
            session,
            action,
            "unban_chat_member",
            chat_id=group.id,
            user_id=action.user_id,
            only_if_banned=True,
        )
        member.owned_restriction = None
