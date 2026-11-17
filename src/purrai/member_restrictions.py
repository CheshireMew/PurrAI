"""Owned member restriction mutations and recovery after lost receipts."""

from sqlalchemy import func, select

from purrai.action_queue import enqueue
from purrai.action_execution import RestrictionPreserved
from purrai.models import (
    Action,
    Member,
    Violation,
    now,
    uid,
)
from purrai.restriction_state import ban_matches, restriction_matches, permission_state_matches, moderation_ownership, schedule_moderation_expiry
from purrai.rule_evaluation import rule_penalty_applies
from purrai.telegram import ALL_PERMISSIONS, is_admin, is_present, muted_permissions, permission_snapshot
from purrai.verification_penalties import active_verifications, begin_penalty, finish_penalty
from purrai.message_identity import display_name


class MemberRestrictions:
    def __init__(self, transport, emoji_status):
        self.action_call, self.emoji_status = transport.call, emoji_status

    async def ban_emoji_status(self, session, group, member, action):
        return await self.emoji_penalty(session, group, member, action, ban=True)

    async def mute_emoji_status(self, session, group, member, action):
        return await self.emoji_penalty(session, group, member, action, ban=False)

    async def emoji_penalty(self, session, group, member, action, *, ban):
        if not await self.emoji_status.eligible(session, action):
            action.state = "cancelled"
            return
        owned = (member.owned_restriction if member else None) or {}
        current, recovered = None, False
        if ban or owned.get("source_action") == action.id:
            current = await self.action_call(
                session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id
            )
            recovered = owned.get("source_action") == action.id and (
                ban_matches(current, 0) if ban else current.get("status") == "restricted"
                and permission_state_matches(current, owned.get("permissions"), 0, tolerance=0)
            )
        # Reconcile an accepted mutation before looking at a subsequently changed emoji status.
        if not recovered:
            result = await self.emoji_status.resolve(
                action.user_id, call=lambda method, **kw: self.action_call(session, action, method, **kw)
            )
            if result["pack"] not in group.settings["emoji_status_blocked_packs"]:
                action.state = "cancelled"
                return
            action.payload = {**action.payload, **result}
            current = None
        if ban:
            await self.permanent_ban(session, group, member, action, current=current)
        else:
            await self.permanent_profile_mute(session, group, member, action, "emoji_status", current=current)

    async def permanent_ban(self, session, group, member, action, *, current=None):
        if current is None:
            current = await self.action_call(
                session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id
            )
        if is_admin(current) or (member.restriction_conflict
                                and (member.owned_restriction or {}).get("source_action") == action.id):
            raise RestrictionPreserved()
        if "message_evidence" in action.payload:
            from purrai.rule_evaluation import message_penalty_applies
            from purrai.action_execution import ActionSuperseded
            if not await message_penalty_applies(session, action, group):
                raise ActionSuperseded(reason="消息内容或审核配置已变化，取消旧处罚", before_request=True)
        owned = member.owned_restriction or {}
        reconciled = bool(
            owned.get("kind") == "ban" and owned.get("source_action") == action.id
            and owned.get("until_date") == 0 and ban_matches(current, 0)
        )
        if not reconciled and current.get("status") == "kicked":
            raise RestrictionPreserved("成员已有其他封禁，保留当前封禁")
        active = await active_verifications(session, member)
        violation_label = action.reason if action.kind == "identity_check" else "广告引流违规" if action.kind == "advertisement_ban" else "状态表情违规"
        member.restriction_conflict = False
        member.owned_restriction = {
            "kind": "ban", "source_action": action.id, "until_date": 0,
            "automatic_permanent": True,
            "pack": action.payload.get("pack", ""),
            **({"verification_id": active[0].id} if active else {}),
        }
        begin_penalty(active, violation_label, "permanent_ban")
        if not reconciled:
            await self.action_call(
                session, action, "ban_chat_member", chat_id=group.id,
                user_id=member.user_id, until_date=0,
            )
        member.present = False
        await finish_penalty(session, active, violation_label, "permanent_ban")
        if action.kind == "emoji_status_ban":
            await self.record_emoji_violation(session, group, member, action, "永久封禁")

    async def verification_mute(self, session, action, group, verification, member):
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id
        )
        if member.restriction_conflict:
            raise RestrictionPreserved()
        if not is_present(current) or is_admin(current):
            raise RestrictionPreserved("成员已离群或成为管理员，取消旧验证任务")
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
            raise RestrictionPreserved()
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
        owned = moderation_ownership(member, current, action)
        already_muted = bool(
            owned
            and owned.get("kind") in ("profile", "emoji_status", "rule", "ai_ad", "sample_ad")
            and owned.get("until_date") == 0
            and current.get("status") == "restricted"
            and permission_state_matches(current, owned.get("permissions"), 0, tolerance=0)
        )
        active = await active_verifications(session, member)
        chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
        recovered = already_muted and owned.get("source_action") == action.id
        if not recovered and "advertisement_evidence" in action.payload and "ai_keyword_evidence" not in action.payload:
            from purrai.advertisement_store import evidence_applies
            if not await evidence_applies(session, action, group):
                action.state, action.error = "cancelled", "广告证据或用户资料已变化，取消旧处罚"
                return
        if kind == "ai_ad" and "ai_keyword_evidence" in action.payload and not recovered:
            from purrai.ai_advertising import keyword_penalty_applies
            if not await keyword_penalty_applies(session, action, group):
                action.state, action.error = "cancelled", "AI广告证据或关键词规则已变化，取消旧处罚"
                return
        if kind in ("profile", "rule") and not recovered and not await rule_penalty_applies(session, action, group):
            action.state, action.error = "cancelled", "审核规则已变化，不再执行永久禁言"
            return
        previous = (owned or {}).get("previous", {})
        if owned and owned.get("kind") == "verification" and active:
            previous = active[0].previous_permissions
        elif not owned:
            previous = {**permission_snapshot(current if current.get("status") == "restricted" else chat.get("permissions", {})),
                        "__until_date": current.get("until_date", 0)}
        target = muted_permissions(current if current.get("status") == "restricted" else chat.get("permissions", {}))
        member.restriction_conflict = False
        member.owned_restriction = {
            "kind": kind,
            "source_action": action.id,
            "permissions": target,
            "until_date": 0,
            "previous": previous,
            "before": (owned or {}).get("before") if (owned or {}).get("source_action") == action.id else {
                "permissions": permission_snapshot(current), "until_date": current.get("until_date", 0)},
            "before_status": (owned or {}).get("before_status") if (owned or {}).get("source_action") == action.id else current.get("status"),
            "pack": action.payload.get("pack", ""),
            **({"verification_id": active[0].id} if active else {}),
        }
        await schedule_moderation_expiry(session, group, member)
        ai_label = "AI确认广告且命中关键词" if "ai_keyword_evidence" in action.payload else action.reason or "AI累计广告处理"
        violation_label = ("已确认广告样本匹配" if kind == "sample_ad" else ai_label if kind == "ai_ad"
                           else "正文广告规则违规" if kind == "rule" else "昵称、简介或状态表情违规")
        begin_penalty(active, violation_label, "permanent_mute")
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
        await finish_penalty(session, active, violation_label, "permanent_mute")
        if kind == "ai_ad" and group.settings.get("ai_ad_mute_notice", False):
            name = display_name(current.get("user") or {}, fallback=action.payload.get("user_name") or "未命名成员")
            await enqueue(session, "warn", f"ai-ad-notice:{action.id}", group.id, member.user_id,
                {"text": f"用户{name}：{ai_label}，已永久禁言。"})
        if kind != "emoji_status":
            return
        await self.record_emoji_violation(session, group, member, action, "永久禁言")

    async def record_emoji_violation(self, session, group, member, action, penalty):
        violation = Violation(
            id=uid(),
            group_id=group.id,
            user_id=member.user_id,
            message_id=-int(action.id[:15], 16),
            excerpt="名字旁状态表情检查",
            reasons=[f"状态表情包黑名单：{action.payload['pack']}；{penalty}"],
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
                    Action.target_message_id == message_id,
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

    async def kick_member(self, session, group, member, action, kind, *, allow_existing_restriction=False):
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id
        )
        if is_admin(current) or member.restriction_conflict and not allow_existing_restriction:
            raise RestrictionPreserved()
        if is_present(current):
            if action.actor_id is None and not group.enabled:
                action.state, action.error = "cancelled", "群保护已暂停，已停止移出成员"
                return
            if action.payload.get("kick_until") and (
                permission_snapshot(current) != action.payload.get("before_kick_permissions")
                or current.get("status") != action.payload.get("before_kick_status")
                or current.get("until_date", 0) != action.payload.get("before_kick_until", 0)
            ):
                raise RestrictionPreserved("移出重试前成员状态已变化，保留当前状态")
            until = int(now() + 600)
            action.payload = {
                **action.payload,
                "kick_until": until,
                "before_kick_permissions": permission_snapshot(current),
                "before_kick_status": current.get("status"),
                "before_kick_until": current.get("until_date", 0),
            }
            member.restriction_conflict = False
            member.owned_restriction = {"kind": kind, "until_date": until}
            await self.action_call(
                session, action, "ban_chat_member", chat_id=group.id, user_id=member.user_id, until_date=until
            )
        elif current.get("status") == "kicked":
            if (
                not action.payload.get("kick_until")
                or abs(current.get("until_date", 0) - action.payload["kick_until"]) > 2
            ):
                raise RestrictionPreserved("成员存在其他封禁，保留当前封禁")
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
