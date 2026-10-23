"""Owned member restriction mutations and recovery after lost receipts."""

import secrets

from sqlalchemy import func, select

from purrai.action_queue import enqueue
from purrai.models import (
    Action,
    Member,
    Verification,
    Violation,
    now,
    uid,
)
from purrai.restriction_state import restriction_matches, permission_state_matches
from purrai.telegram import ALL_PERMISSIONS, is_admin, is_present, muted_permissions, permission_snapshot
from purrai.verification_cleanup import cleanup
from purrai.verification_state import ACTIVE


class MemberRestrictions:
    def __init__(self, transport, emoji_status):
        self.action_call, self.emoji_status = transport.call, emoji_status

    async def verification_mute(self, session, action, group, verification, member):
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id
        )
        if member.restriction_conflict:
            raise ValueError("管理员已经修改成员权限，需要人工处理")
        if not is_present(current) or is_admin(current):
            raise ValueError("成员已经离群或成为管理员，需要人工核实")
        owned = member.owned_restriction
        if (
            owned
            and current.get("status") == "restricted"
            and (
                (not restriction_matches(current, owned))
                and not permission_state_matches(
                    current,
                    {k: verification.previous_permissions.get(k, False) for k in ALL_PERMISSIONS},
                    verification.previous_permissions.get("__until_date", 0),
                )
            )
        ):
            raise ValueError("成员已有其他限制，需要人工处理")
        if not owned:
            before = current
            if current["status"] != "restricted":
                chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
                before = chat.get("permissions", {})
            previous = permission_snapshot(before)
            verification.previous_permissions = {**previous, "__until_date": current.get("until_date", 0)}
            owned = {"permissions": muted_permissions(previous), "until_date": 0}
        already_muted = bool(
            action.kind == "verify_mute" and member.owned_restriction
            and member.owned_restriction.get("kind") in ("verification", "verification_failed")
            and current.get("status") == "restricted" and restriction_matches(current, owned)
        )
        member.owned_restriction = {**owned, "kind": "verification", "verification_id": verification.id}
        if already_muted:
            return
        await self.action_call(
            session,
            action,
            "restrict_chat_member",
            chat_id=group.id,
            user_id=member.user_id,
            permissions=owned["permissions"],
            use_independent_chat_permissions=True,
            until_date=0,
        )

    async def mute_emoji_status(self, session, group, member, action):
        if not await self.emoji_status.eligible(session, action):
            action.state = "cancelled"
            return
        owned = member.owned_restriction if member else None
        if owned and owned.get("source_action") == action.id:
            current = await self.action_call(
                session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id
            )
            if current.get("status") == "restricted" and permission_state_matches(
                current, owned.get("permissions"), 0, tolerance=0
            ):
                # A lost response must not leave verification stuck if the user
                # changes their status after Telegram already applied our mute.
                await self.permanent_profile_mute(
                    session, group, member, action, "emoji_status", current=current
                )
                return
        result = await self.emoji_status.resolve(
            action.user_id, call=lambda method, **kw: self.action_call(session, action, method, **kw)
        )
        if result["pack"] not in group.settings["emoji_status_blocked_packs"]:
            action.state = "cancelled"
            return
        action.payload = {**action.payload, **result}
        await self.permanent_profile_mute(session, group, member, action, "emoji_status")

    async def permanent_profile_mute(self, session, group, member, action, kind, *, current=None):
        if current is None:
            current = await self.action_call(
                session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id
            )
        if not is_present(current) or is_admin(current):
            action.state = "cancelled"
            return
        if member is None:
            member = Member(group_id=group.id, user_id=action.user_id, joined_at=0)
            session.add(member)
            await session.flush()
        owned = member.owned_restriction
        already_muted = bool(
            owned
            and owned.get("kind") in ("profile", "emoji_status", "rule")
            and owned.get("until_date") == 0
            and current.get("status") == "restricted"
            and permission_state_matches(current, owned.get("permissions"), 0, tolerance=0)
        )
        if member.restriction_conflict or current.get("status") == "restricted" and not owned:
            raise ValueError("成员存在其他管理员限制，保留当前限制")
        if owned and current.get("status") == "restricted" and (not restriction_matches(current, owned)):
            raise ValueError("成员限制已被其他管理员修改，保留当前限制")
        active = (
            await session.scalars(
                select(Verification).where(
                    Verification.group_id == group.id,
                    Verification.user_id == member.user_id,
                    Verification.generation == member.generation,
                    Verification.state.in_(ACTIVE),
                )
            )
        ).all()
        chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
        previous = (owned or {}).get("previous", {})
        if owned and owned.get("kind") == "verification" and active:
            previous = active[0].previous_permissions
        elif not owned:
            previous = {**permission_snapshot(chat.get("permissions", {})), "__until_date": 0}
        target = muted_permissions(previous or chat.get("permissions", {}))
        member.owned_restriction = {
            "kind": kind,
            "source_action": action.id,
            "permissions": target,
            "until_date": 0,
            "previous": previous,
            "pack": action.payload.get("pack", ""),
            **({"verification_id": active[0].id} if active else {}),
        }
        for verification in active:
            verification.state = "failing"
            verification.nonce = secrets.token_hex(4)
            verification.error = (
                "正文广告规则违规" if kind == "rule" else "昵称或状态表情违规"
            ) + "；等待永久禁言完成"
        if not already_muted:
            await self.action_call(
                session,
                action,
                "restrict_chat_member",
                chat_id=group.id,
                user_id=member.user_id,
                permissions=target,
                use_independent_chat_permissions=True,
                until_date=0,
            )
        # Report completion only after Telegram accepted the permanent restriction.
        for verification in active:
            verification.state, verification.failure_action, verification.updated_at = (
                "failed",
                "permanent_mute",
                now(),
            )
            verification.error = (
                "正文广告规则违规" if kind == "rule" else "昵称或状态表情违规"
            ) + "，已永久禁言"
            await cleanup(session, verification)
        if kind != "emoji_status":
            return
        violation = Violation(
            id=uid(),
            group_id=group.id,
            user_id=member.user_id,
            message_id=-int(action.id[:15], 16),
            excerpt="名字旁状态表情检查",
            reasons=[f"状态表情包黑名单：{action.payload['pack']}；永久禁言"],
            profile_violation=True,
            message_violation=False,
            warning_active=False,
        )
        session.add(violation)
        await session.flush()
        action.payload = {**action.payload, "violation_id": violation.id}
        for message_id in action.payload.get("message_ids", []):
            existing = await session.scalar(
                select(Action.id)
                .where(
                    Action.group_id == group.id,
                    Action.kind == "delete",
                    Action.state != "cancelled",
                    func.coalesce(Action.payload["chat_id"].as_integer(), Action.group_id) == group.id,
                    Action.payload["message_id"].as_integer() == message_id,
                )
                .limit(1)
            )
            if existing:
                continue
            await enqueue(
                session,
                "delete",
                f"emoji-message:{group.id}:{message_id}",
                group.id,
                member.user_id,
                {"message_id": message_id, "violation_id": violation.id},
                0,
            )

    async def kick_member(self, session, group, member, action, kind):
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id
        )
        if is_admin(current) or member.restriction_conflict:
            raise ValueError("成员身份或权限已变化，需要人工处理")
        if is_present(current):
            if action.actor_id is None and not group.enabled:
                action.state, action.error = "cancelled", "群保护已暂停，已停止移出成员"
                return
            if action.payload.get("kick_until") and (
                permission_snapshot(current) != action.payload.get("before_kick_permissions")
                or current.get("status") != action.payload.get("before_kick_status")
                or current.get("until_date", 0) != action.payload.get("before_kick_until", 0)
            ):
                raise ValueError("移出重试前成员状态已变化，不重复踢出")
            until = int(now() + 600)
            action.payload = {
                **action.payload,
                "kick_until": until,
                "before_kick_permissions": permission_snapshot(current),
                "before_kick_status": current.get("status"),
                "before_kick_until": current.get("until_date", 0),
            }
            member.owned_restriction = {"kind": kind, "until_date": until}
            await self.action_call(
                session, action, "ban_chat_member", chat_id=group.id, user_id=member.user_id, until_date=until
            )
        elif current.get("status") == "kicked":
            if (
                not action.payload.get("kick_until")
                or abs(current.get("until_date", 0) - action.payload["kick_until"]) > 2
            ):
                raise ValueError("成员存在其他封禁，不自动解除")
        # Telegram implements removal as ban + unban. A retry only clears this
        # action's temporary ban; a lost response must never remove a new join.
        if current.get("status") != "left":
            await self.action_call(
                session,
                action,
                "unban_chat_member",
                chat_id=group.id,
                user_id=member.user_id,
                only_if_banned=True,
            )
        member.owned_restriction, member.present = None, False
