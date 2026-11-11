"""Message assessment, evidence recording and penalty decisions."""

import copy

from sqlalchemy import select

from purrai.shared_policy import effective_rules
from purrai.solicitation_rules import contact_should_block
from purrai.action_queue import enqueue
from purrai.advertising_guard import message_links
from purrai.archive import capture, mark_reasons
from purrai.group_link_identity import own_channel_id, remember_linked_channel
from purrai.media_gate import remember_text, restricted_newcomer, text_count_before, visual_media
from purrai.message_activity import observe_activity
from purrai.message_identity import message_sender
from purrai.models import (
    Group,
    Member,
    Verification,
    Violation,
    Whitelist,
    now,
    uid,
)
from purrai.moderation_penalties import apply_penalty
from purrai.moderation_records import record_rule_hits, record_violation
from purrai.rules import activity_reasons, message_content
from purrai.rule_evaluation import prepare_message
from purrai.sender_identity import sender_name
from purrai.telegram import is_admin
from purrai.telegram_sources import channel_sources, external_channel_reasons
from purrai.verification_state import ACTIVE


class Moderation:
    def __init__(self, db, telegram, settings, trust, emoji_status, advertising):
        self.db, self.tg, self.settings = db, telegram, settings
        self.trust, self.emoji_status = trust, emoji_status
        self.advertising = advertising

    async def profile_on_join(self, session, group, member, user, assessment):
        if user["id"] in self.settings.superadmin_ids:
            return False
        white = await session.get(Whitelist, (group.id, user["id"]))
        if white and white.moderation:
            return False
        profile = sender_name(user, include_username=True)
        if assessment is None or not await assessment.current(session, group):
            raise ConnectionError("群规则正在变化，稍后重新核实入群昵称")
        result = assessment.profile
        if not result["name_reasons"] and not result["ban_reasons"]:
            return False
        violation = Violation(
            id=uid(),
            group_id=group.id,
            user_id=user["id"],
            message_id=-int(member.generation[:15], 16),
            sender_name=profile,
            excerpt="入群昵称检查",
            reasons=list(dict.fromkeys(result["name_reasons"] + result["ban_reasons"])),
            profile_violation=bool(result["name_reasons"]),
            message_violation=False,
            warning_active=False,
        )
        session.add(violation)
        await session.flush()
        await record_rule_hits(session, group.id, violation.message_id, result.get("rule_ids", []))
        await apply_penalty(session, group, violation, member, evidence={"message": {
            "from": user, "chat": {"id": group.id, "type": group.type}, "text": ""}})
        return True

    async def message(self, message: dict, edited=False, update_id=0, archived_at_receipt=False):
        # Commit before exemptions, moderation lookups or deletion can fail.
        # Runtime already made the recording decision at receipt, even when it was off.
        if not archived_at_receipt:
            async with self.db.transaction() as session:
                await capture(session, message, edited, update_id, settings=self.settings)
        if message["chat"]["type"] not in ("supergroup", "group"):
            return
        if message.get("new_chat_members") or message.get("left_chat_member"):
            return
        group_id = message["chat"]["id"]
        sender = message_sender(message)
        if not sender or sender.get("id") == self.tg.user_id:
            return
        needs_admin_check = await self.assess_message(message, edited, update_id)
        if not needs_admin_check:
            return
        # Check a potential violation outside the database writer. The second
        # pass reloads current configuration, exemptions and verification state;
        # an existing message fingerprint never counts as another message.
        if is_admin(await self.tg.call("get_chat_member", chat_id=group_id, user_id=sender["id"])):
            async with self.db.transaction() as session:
                await self.trust.exempt_administrator(session, group_id, sender["id"])
            return
        own = await self.tg.call("get_chat", chat_id=group_id, request_timeout=3) if channel_sources(message) else {}
        await self.assess_message(message, edited, update_id, admin_checked=True, own=own)

    async def assess_message(self, message, edited, update_id, *, admin_checked=False, own=None):
        # A configuration edit during CPU work invalidates the entire assessment,
        # before fingerprints, evidence or penalties are written.
        for _ in range(3):
            assessment = await prepare_message(self.db, message)
            if assessment is None:
                return
            async with self.db.transaction() as session:
                group = await session.get(Group, message["chat"]["id"])
                if not await assessment.current(session, group):
                    continue
                if own is not None:
                    await remember_linked_channel(session, group.id, own)
                return await self.moderate_message(session, message, edited, update_id,
                                                   admin_checked=admin_checked, assessment=assessment)
        raise ConnectionError("群规则正在变化，稍后重新审核消息")

    async def moderate_message(self, session, message, edited, update_id, *, admin_checked=False, assessment):
        group_id = message["chat"]["id"]
        sender_chat = message.get("sender_chat")
        sender = message_sender(message)
        group = await session.get(Group, group_id)
        if not group or not group.approved or not group.enabled:
            return
        if sender_chat and (sender_chat["id"] == group_id or message.get("is_automatic_forward")):
            return
        user_id = sender["id"]
        member = await session.get(Member, (group_id, user_id))
        if not sender_chat:
            white = await session.get(Whitelist, (group_id, user_id))
            if user_id in self.settings.superadmin_ids:
                return
            if white and white.moderation:
                # A moderation-only exemption must preserve the separate
                # verification setting. A manual unmute enables both flags.
                if not white.verification and await session.scalar(
                    select(Verification.id).where(
                        Verification.group_id == group_id,
                        Verification.user_id == user_id,
                        Verification.state.in_(ACTIVE),
                    )
                ):
                    if not admin_checked:
                        return True
                    await enqueue(
                        session,
                        "delete",
                        f"pending-message:{group_id}:{message['message_id']}",
                        group_id,
                        user_id,
                        {"message_id": message["message_id"]},
                        priority=0,
                    )
                return
            if group.settings.get("external_group_ban", True) and channel_sources(message) and not admin_checked:
                return True
            if any(group.settings.get(key, True) for key in ("external_group_ban", "external_invite_ban")) and message_links(message):
                if not admin_checked:
                    return True
                await self.advertising.queue(session, group, member, message, update_id, edited)
                member = await session.get(Member, (group_id, user_id))
            text, urls = message_content(message)
            display_name = sender_name(sender, include_username=True)
            rules = await effective_rules(session, group_id)
            profile = copy.deepcopy(assessment.profile)
            from purrai.promotion_targets import blacklist_reasons
            target_reasons = blacklist_reasons(message, rules, settings=group.settings)
            profile["reasons"] += target_reasons
            profile["ban_reasons"] += [reason for reason in target_reasons if reason.startswith("全局广告黑名单：")]
            if group.settings.get("external_group_ban", True):
                channel_reasons = external_channel_reasons(
                    channel_sources(message), group_id, group.settings, rules,
                    linked_channel_id=await own_channel_id(session, group_id),
                )
                profile["reasons"] += channel_reasons
                profile["ban_reasons"] += channel_reasons
            if contact_should_block(message, group.settings, rules) or profile["name_reasons"] or profile["mute_reasons"] or profile["ban_reasons"]:
                if not admin_checked:
                    return True
                await self.record_member_message(
                    session, group, member, message, edited, update_id, display_name, profile
                )
                return
            if (
                group.settings.get("emoji_status_blocked_packs")
                and sender.get("is_premium")
                and not admin_checked
            ):
                return True
            await self.emoji_status.queue(session, group, sender, message["message_id"])
            verification = await session.scalar(
                select(Verification).where(
                    Verification.group_id == group_id,
                    Verification.user_id == user_id,
                    Verification.state.in_(ACTIVE),
                )
            )
            if verification:
                if not admin_checked:
                    return True
                await enqueue(
                    session,
                    "delete",
                    f"pending-message:{group_id}:{message['message_id']}",
                    group_id,
                    user_id,
                    {"message_id": message["message_id"]},
                    priority=0,
                )
                return
            minimum = group.settings.get("media_min_text_messages", 3)
            if minimum and restricted_newcomer(member, now()) and visual_media(message):
                count = await text_count_before(session, message, minimum)
                if count < minimum:
                    if not admin_checked:
                        return True
                    await self.delete_early_media(
                        session, group, member, message, edited, update_id, count, minimum
                    )
                    return
        text, urls = message_content(message)
        activity = await observe_activity(session, group, member, message, text, edited, assessment)
        rules = await effective_rules(session, group_id)
        display_name = sender_name(sender, include_username=True)
        result = copy.deepcopy(assessment.newcomer if activity.newcomer else assessment.ordinary)
        from purrai.coordinated_protection import coordinated_reason, emergency_state
        coordinated = await coordinated_reason(session, group, message, activity)
        if coordinated:
            result["reasons"].append(coordinated)
        if activity.newcomer and (urls or message_links(message)) and (await emergency_state(session, group_id)).get("active"):
            result["reasons"].append("临时应急保护：新人链接消息暂时拦截")
        result["reasons"].extend(activity_reasons(group.settings, activity.repeats, activity.similar, activity.burst))
        reasons = list(dict.fromkeys(result["reasons"] + result["name_reasons"]))
        if sender_chat and result["name_reasons"]:
            result["reasons"] += ["频道身份昵称命中，删除该消息"]
            reasons = list(dict.fromkeys(result["reasons"] + result["name_reasons"]))
        if not reasons:
            if not sender_chat:
                await remember_text(session, message, edited)
            return
        if not sender_chat and not admin_checked:
            return True
        await mark_reasons(session, message, edited, update_id, reasons)
        violation, changed = await record_violation(
            session,
            group_id=group_id,
            user_id=user_id,
            message_id=message["message_id"],
            reasons=reasons,
            excerpt=text,
            sender_name=display_name,
            sender_chat=bool(sender_chat),
            profile_violation=bool(result["name_reasons"]),
            rule_mute=bool(result["mute_reasons"]),
            message_violation=bool(result["reasons"]),
        )
        if changed:
            await apply_penalty(session, group, violation, member, evidence={"message": message, "links": []})
        await record_rule_hits(session, group.id, message["message_id"], result.get("rule_ids", []))

    async def record_member_message(
        self, session, group, member, message, edited, update_id, display_name, result
    ):
        reasons = list(dict.fromkeys(result["name_reasons"] + result["reasons"]))
        await mark_reasons(session, message, edited, update_id, reasons)
        violation, changed = await record_violation(
            session,
            group_id=group.id,
            user_id=message_sender(message)["id"],
            message_id=message["message_id"],
            reasons=reasons,
            sender_name=display_name,
            excerpt=message.get("text") or message.get("caption") or "",
            profile_violation=bool(result["name_reasons"]),
            rule_mute=bool(result["mute_reasons"]),
            message_violation=True,
        )
        if changed:
            await apply_penalty(session, group, violation, member, evidence={"message": message, "links": []})
        await record_rule_hits(session, group.id, message["message_id"], result.get("rule_ids", []))

    async def delete_early_media(self, session, group, member, message, edited, update_id, count, minimum):
        reason = f"独立文字发言不足 {minimum} 条（已记录 {count} 条），删除图片或表情整条消息"
        await mark_reasons(session, message, edited, update_id, [reason])
        sender = message_sender(message)
        violation, _ = await record_violation(
            session,
            group_id=group.id,
            user_id=sender["id"],
            message_id=message["message_id"],
            reasons=[reason],
            excerpt=message.get("text") or message.get("caption") or "",
            sender_name=sender_name(sender, include_username=True),
            message_violation=True,
        )
        # This gate always deletes the complete message, including its caption.
        # Do not invoke profile penalties or create warnings for this rule.
        await enqueue(
            session,
            "delete",
            f"delete:{violation.id}",
            group.id,
            sender["id"],
            {
                "message_id": message["message_id"],
                "violation_id": violation.id,
                "media_gate": True,
                "generation": member.generation,
            },
            1,
        )
