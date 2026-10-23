"""Message assessment, evidence recording and penalty decisions."""

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.archive import capture, mark_reasons
from purrai.media_gate import remember_text, restricted_newcomer, text_count_before, visual_media
from purrai.message_activity import observe_activity
from purrai.models import (
    Group,
    Member,
    Rule,
    Verification,
    Violation,
    Whitelist,
    now,
    uid,
)
from purrai.moderation_penalties import apply_penalty
from purrai.moderation_records import record_violation
from purrai.rules import assess, message_content
from purrai.serialization import row_dict
from purrai.telegram import is_admin
from purrai.verification_state import ACTIVE


class Moderation:
    def __init__(self, db, telegram, settings, trust, emoji_status):
        self.db, self.tg, self.settings = db, telegram, settings
        self.trust, self.emoji_status = trust, emoji_status

    async def profile_on_join(self, session, group, member, user):
        if user["id"] in self.settings.superadmin_ids:
            return False
        white = await session.get(Whitelist, (group.id, user["id"]))
        if white and white.moderation:
            return False
        profile = " ".join(
            [
                str(user.get("first_name", "")),
                str(user.get("last_name", "")),
                "@" + user["username"] if user.get("username") else "",
            ]
        ).strip()[:500]
        rules = (await session.scalars(select(Rule).where(Rule.group_id == group.id))).all()
        result = assess("", [], [row_dict(row) for row in rules], group.settings, sender_name=profile)
        if not result["name_reasons"]:
            return False
        violation = Violation(
            id=uid(),
            group_id=group.id,
            user_id=user["id"],
            message_id=-int(member.generation[:15], 16),
            sender_name=profile,
            excerpt="入群昵称检查",
            reasons=result["name_reasons"],
            profile_violation=True,
            message_violation=False,
            warning_active=False,
        )
        session.add(violation)
        await session.flush()
        await apply_penalty(session, group, violation, member)
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
        sender_chat = message.get("sender_chat")
        sender = sender_chat or message.get("from")
        if not sender or sender.get("id") == self.tg.user_id:
            return
        async with self.db.transaction() as session:
            needs_admin_check = await self.moderate_message(session, message, edited, update_id)
        if not needs_admin_check:
            return
        # Check a potential violation outside the database writer. The second
        # pass reloads current configuration, exemptions and verification state;
        # an existing message fingerprint never counts as another message.
        if is_admin(await self.tg.call("get_chat_member", chat_id=group_id, user_id=sender["id"])):
            async with self.db.transaction() as session:
                await self.trust.exempt_administrator(session, group_id, sender["id"])
            return
        async with self.db.transaction() as session:
            await self.moderate_message(session, message, edited, update_id, admin_checked=True)

    async def moderate_message(self, session, message, edited, update_id, *, admin_checked=False):
        group_id = message["chat"]["id"]
        sender_chat = message.get("sender_chat")
        sender = sender_chat or message["from"]
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
            text, urls = message_content(message)
            display_name = " ".join(str(sender.get(key, "")) for key in ("first_name", "last_name")).strip()
            if sender.get("username"):
                display_name += " @" + sender["username"]
            rules = (await session.scalars(select(Rule).where(Rule.group_id == group_id))).all()
            profile = assess(
                text,
                urls,
                [row_dict(rule) for rule in rules],
                group.settings,
                sender_name=display_name,
                contact=bool(message.get("contact")),
            )
            if message.get("contact") or profile["name_reasons"] or profile["mute_reasons"]:
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
        activity = await observe_activity(session, group, member, message, text, edited)
        rules = (await session.scalars(select(Rule).where(Rule.group_id == group_id))).all()
        display_name = " ".join(
            str(sender.get(key, "")) for key in ("first_name", "last_name", "title")
        ).strip()[:500]
        if sender.get("username"):
            display_name = (display_name + " @" + sender["username"])[:500]
        result = assess(
            text,
            urls,
            [row_dict(r) for r in rules],
            group.settings,
            activity.newcomer,
            bool(message.get("contact")),
            activity.repeats,
            activity.similar,
            activity.burst,
            display_name,
            forwarded=bool(message.get("forward_origin") or message.get("forward_date")
                           or message.get("forward_from") or message.get("forward_from_chat")
                           or message.get("forward_sender_name")),
            mentioned_users=[entity.get("user", {}) for entity in
                             (message.get("entities") or message.get("caption_entities") or [])
                             if entity.get("type") == "text_mention"],
        )
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
            await apply_penalty(session, group, violation, member)

    async def record_member_message(
        self, session, group, member, message, edited, update_id, display_name, result
    ):
        reasons = list(dict.fromkeys(result["name_reasons"] + result["reasons"]))
        await mark_reasons(session, message, edited, update_id, reasons)
        violation, changed = await record_violation(
            session,
            group_id=group.id,
            user_id=message["from"]["id"],
            message_id=message["message_id"],
            reasons=reasons,
            sender_name=display_name,
            excerpt=message.get("text") or message.get("caption") or "",
            profile_violation=bool(result["name_reasons"]),
            rule_mute=bool(result["mute_reasons"]),
            message_violation=True,
        )
        if changed:
            await apply_penalty(session, group, violation, member)

    async def delete_early_media(self, session, group, member, message, edited, update_id, count, minimum):
        reason = f"独立文字发言不足 {minimum} 条（已记录 {count} 条），删除图片或表情整条消息"
        await mark_reasons(session, message, edited, update_id, [reason])
        sender = message["from"]
        violation, _ = await record_violation(
            session,
            group_id=group.id,
            user_id=sender["id"],
            message_id=message["message_id"],
            reasons=[reason],
            excerpt=message.get("text") or message.get("caption") or "",
            sender_name=" ".join(sender.get(key, "") for key in ("first_name", "last_name")).strip(),
            message_violation=True,
        )
        # This gate always deletes the complete message, including its caption.
        # Do not invoke profile penalties or create warnings for this rule.
        await enqueue(
            session,
            "delete",
            f"delete:{violation.id}",
            group.id,
            message["from"]["id"],
            {
                "message_id": message["message_id"],
                "violation_id": violation.id,
                "media_gate": True,
                "generation": member.generation,
            },
            1,
        )
