"""Explicit administrator restrictions, restoration and ownership checks."""

from purrai.action_queue import enqueue
from purrai.action_execution import RestrictionPreserved
from purrai.message_identity import display_name
from purrai.models import (
    Action,
    Verification,
    Fingerprint,
    Member,
    Violation,
    now,
)
from purrai.restriction_state import (
    restriction_matches, permission_state_matches, ban_matches, owns_verification_ban,
    moderation_ownership, schedule_moderation_expiry,
)
from purrai.telegram import ALL_PERMISSIONS, SEND_PERMISSIONS, is_admin, is_present, muted_permissions, permission_snapshot
from purrai.verification_cleanup import cleanup
from purrai.verification_state import matches_action


class RestrictionActions:
    def __init__(self, transport, trust):
        self.action_call, self.trust = transport.call, trust

    async def prepare(self, session, action, group, verification, member):
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
                return False
        if action.kind in ("unmute", "unban") and action.payload.get("violation_id"):
            owned = member.owned_restriction if member else None
            source = (
                await session.get(Action, owned["source_action"])
                if owned and owned.get("source_action")
                else None
            )
            if not source or source.payload.get("violation_id") != action.payload["violation_id"]:
                action.state, action.error = "cancelled", "该违规对应的限制已被替换，已停止旧解除任务"
                return False
        if not matches_action(action, verification, member):
            action.state = "cancelled"
            return False
        return True

    async def restore(self, session, action, group, verification, member):
        if action.payload.get("expire_previous"):
            return await self.expire_previous(session, action, group, member)
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
            raise RestrictionPreserved()
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
            raise RestrictionPreserved()
        chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
        defaults = chat.get("permissions", {})
        previous = verification.previous_permissions if verification else owned.get("previous", {})
        previous_until = previous.get("__until_date", 0)
        if action.payload.get("automatic_restore") and now() < previous_until < now() + 31:
            # Telegram interprets a restriction expiring in <30 seconds as permanent.
            action.available_at = previous_until + 1
            return False
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

    async def expire_previous(self, session, action, group, member):
        owned = member.owned_restriction
        current = await self.action_call(session, action, "get_chat_member", chat_id=group.id, user_id=member.user_id)
        if member.restriction_conflict or not is_present(current) or is_admin(current):
            raise RestrictionPreserved()
        until = owned.get("until_date", 0)
        if until and until <= now():
            member.owned_restriction = None
            return
        target = owned.get("previous_expiry_permissions")
        accepted = target is not None and permission_state_matches(current, target, until)
        if not accepted and not restriction_matches(current, owned):
            raise RestrictionPreserved()
        if not accepted:
            if until and until < now() + 31:
                action.available_at = until + 1
                return False
            chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
            target = muted_permissions(chat.get("permissions", {}))
            member.owned_restriction = {**owned, "previous_expiry_permissions": target}
            await self.action_call(session, action, "restrict_chat_member", chat_id=group.id, user_id=member.user_id,
                                   permissions=target, use_independent_chat_permissions=True, until_date=until)
        updated = {**member.owned_restriction, "permissions": target, "previous": {}}
        updated.pop("previous_expiry_permissions", None)
        member.owned_restriction = updated

    async def ban(self, session, action, group, verification, member):
        current = await self.action_call(
            session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id
        )
        if is_admin(current) or (member and member.restriction_conflict
                                and (member.owned_restriction or {}).get("source_action") == action.id):
            raise RestrictionPreserved()
        owned = member.owned_restriction if member else None
        reconciled = bool(
            owned and owned.get("source_action") == action.id and ban_matches(current, owned["until_date"])
        )
        if not reconciled:
            if current.get("status") == "kicked":
                raise RestrictionPreserved("成员已有其他封禁，保留当前封禁")
            until = (
                owned["until_date"]
                if owned and owned.get("source_action") == action.id
                else int(now() + action.payload.get("duration_seconds", group.settings["failure_ban_seconds"]))
            )
            if owned and owned.get("source_action") == action.id and until <= now():
                member.owned_restriction = None
                action.payload = {**action.payload, "outcome": "restriction_expired"}
                return
            if member:
                member.restriction_conflict = False
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

    async def spam_mute(self, session, action, group, verification, member):
        if member and (member.owned_restriction or {}).get("source_action") == action.id:
            await self.mute(session, action, group, verification, member)
            return
        fingerprint = await session.get(Fingerprint, (group.id, action.user_id, action.payload["message_id"]))
        settings = action.payload.get("spam_settings", {})
        if (not member or not member.present or not settings or now() - action.created_at > 600
                or not fingerprint or fingerprint.digest != action.payload.get("fingerprint")
                or any(group.settings.get(key) != value for key, value in settings.items())):
            action.state, action.error = "cancelled", "消息或刷屏设置已变化，取消旧处罚"
            return
        await self.mute(session, action, group, verification, member)

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
        from purrai.schemas import GroupSettings
        duration = (GroupSettings.model_validate(group.settings).spam_mute_seconds if action.kind == "spam_mute"
                    else action.payload.get("duration_seconds", group.settings["mute_seconds"]))
        if member and action.payload.get("generation") and member.generation != action.payload["generation"]:
            raise RestrictionPreserved()
        if member is None:
            member = Member(group_id=group.id, user_id=action.user_id, joined_at=now())
            session.add(member)
        owned = moderation_ownership(member, current, action)
        expires = owned.get("restore_at") or owned.get("until_date", 0)
        if owned.get("source_action") == action.id and expires and expires <= now():
            if owned.get("restore_at") and restriction_matches(current, owned):
                await schedule_moderation_expiry(session, group, member)
            else:
                member.owned_restriction = None
            action.payload = {**action.payload, "outcome": "restriction_expired"}
            return
        deadline = int(now() + duration)
        if (owned.get("source_action") != action.id and current.get("status") == "restricted"
                and all(not current.get(key, False) for key in SEND_PERMISSIONS)
                and (not current.get("until_date") or current["until_date"] >= deadline)):
            action.payload = {**action.payload, "outcome": "already_restricted"}
            if action.kind == "manual_mute":
                action.state, action.error = "cancelled", "已有相同或更严格的禁言，保留当前成员权限"
            return
        reconciled = bool(
            owned.get("source_action") == action.id
            and current.get("status") == "restricted"
            and restriction_matches(current, owned)
        )
        if not reconciled:
            chat = await self.action_call(session, action, "get_chat", chat_id=group.id)
            if owned.get("source_action") != action.id:
                before = permission_snapshot(current if current.get("status") == "restricted" else chat.get("permissions", {}))
                previous = owned.get("previous") if owned else {**before, "__until_date": current.get("until_date", 0)}
                previous = previous or {}
                prior_until = previous.get("__until_date", 0)
                retained = any(chat.get("permissions", {}).get(key, False) and not previous.get(key, True)
                               for key in ALL_PERMISSIONS) and (not prior_until or prior_until > now())
                until = (0 if not prior_until else max(deadline, prior_until)) if retained else deadline
                # Keep the original expiry for prior restrictions; restore only our layer at the deadline.
                owned = {"kind": "moderation", "source_action": action.id,
                         "permissions": muted_permissions(before), "until_date": until, "previous": previous,
                         "before": {"permissions": permission_snapshot(current), "until_date": current.get("until_date", 0)},
                         "before_status": current.get("status"),
                         "restore_at": deadline if until == 0 or until > deadline else 0}
            member.owned_restriction, member.restriction_conflict = owned, False
            await schedule_moderation_expiry(session, group, member)
            await self.action_call(
                session,
                action,
                "restrict_chat_member",
                chat_id=group.id,
                user_id=action.user_id,
                permissions=owned["permissions"],
                use_independent_chat_permissions=True,
                until_date=owned["until_date"],
            )
        name = display_name(current.get("user") or {}, fallback="")
        if not name:
            violation = await session.get(Violation, action.payload["violation_id"]) if action.payload.get("violation_id") else None
            name = (violation.sender_name.split(" @", 1)[0].strip() if violation else "") or "未命名成员"
        await enqueue(
            session,
            "warn",
            f"mute-notice:{action.id}",
            group.id,
            action.user_id,
            {
                "text": f"用户{name}：{'重复刷屏' if action.kind == 'spam_mute' else '管理员处置' if action.kind == 'manual_mute' else '累计违规'}，已临时禁言 {duration // 60} 分钟。"
            },
        )

    async def unban(self, session, action, group, verification, member):
        if action.payload.get("request_cooldown"):
            if member:
                member.request_until = 0
                if action.actor_id:
                    await self.trust.trust_member(session, member, action.actor_id, action.reason, exclude_action=action.id)
            return
        if member and member.owned_restriction and member.owned_restriction.get("kind") != "ban":
            return await self.restore(session, action, group, verification, member)
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
            if action.actor_id:
                await self.trust.trust_member(session, member, action.actor_id, action.reason, exclude_action=action.id)
            return
        if member.restriction_conflict or not ban_matches(current, member.owned_restriction["until_date"]):
            raise RestrictionPreserved()
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
        if action.actor_id:
            await self.trust.trust_member(session, member, action.actor_id, action.reason, exclude_action=action.id)
