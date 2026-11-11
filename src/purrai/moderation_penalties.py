"""Convert recorded moderation facts into durable actions with explicit precedence."""

import secrets

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import (
    Action,
    Fingerprint,
    Member,
    Verification,
    now,
)
from purrai.rules import CONTACT_REASON, CONTACT_REASONS, message_content
from purrai.advertising_patterns import is_ban_reason
from purrai.verification_cleanup import cleanup
from purrai.verification_state import ACTIVE

SPAM_REASONS = {"重复刷屏", "近似内容刷屏", "短时间连续发送"}
SPAM_SETTINGS = ("repeat_enabled", "repeat_window", "repeat_count", "similar_enabled", "similar_window",
                 "similar_count", "similar_threshold", "similar_min_length", "burst_enabled", "burst_window", "burst_count")


async def apply_penalty(session, group, violation, member, actor_id=None, reason="", *, evidence=None):
    from purrai.promotion_targets import remember_targets
    await remember_targets(session, group, violation, evidence)
    message = (evidence or {}).get("message")
    text, urls = message_content(message) if message else (violation.excerpt if violation.message_violation else "", [])
    rule_evidence = {"text": text, "urls": urls, "sender_name": violation.sender_name}
    identifiers = []
    if violation.message_violation:
        action = await enqueue(
            session,
            "delete",
            f"delete:{violation.id}",
            group.id,
            violation.user_id,
            {"message_id": violation.message_id, "violation_id": violation.id,
             **({"spam_settings": {key: group.settings.get(key) for key in SPAM_SETTINGS}}
                if set(violation.reasons) <= SPAM_REASONS else {})},
            1,
            actor_id,
            reason,
        )
        identifiers.append(action.id)
    contact = any(item in violation.reasons for item in CONTACT_REASONS)
    advertising = any(is_ban_reason(item) for item in violation.reasons)
    if (contact or advertising) and not violation.sender_chat:
        if member is None:
            member = Member(group_id=group.id, user_id=violation.user_id, joined_at=0)
            session.add(member)
            await session.flush()
        action = await enqueue(
            session,
            "contact_ban" if contact else "advertisement_ban",
            f"contact-kick:{violation.id}" if contact else f"advertisement-ban:{violation.id}",
            group.id,
            violation.user_id,
            {"violation_id": violation.id, "generation": member.generation,
             **({"contact_evidence": evidence} if contact and evidence else {}),
             **({"ad_evidence": evidence} if advertising and evidence else {})},
            0,
            actor_id,
            reason or (CONTACT_REASON if contact else "；".join(violation.reasons)),
        )
        identifiers.append(action.id)
        if action.state == "pending" and action.payload.get("generation") == member.generation:
            for verification in await session.scalars(
                select(Verification).where(
                    Verification.group_id == group.id,
                    Verification.user_id == violation.user_id,
                    Verification.generation == member.generation,
                    Verification.state.in_(ACTIVE),
                )
            ):
                verification.state, verification.error, verification.updated_at = (
                    "cancelled",
                    CONTACT_REASON if contact else "广告引流违规，等待永久封禁",
                    now(),
                )
                verification.nonce = secrets.token_hex(4)
                await cleanup(session, verification)
    elif (violation.profile_violation or violation.rule_mute) and not violation.sender_chat:
        # Nickname violations retain precedence when both kinds of evidence match.
        profile = violation.profile_violation
        kind = "profile_mute" if profile else "rule_mute"
        if member is None:
            member = Member(group_id=group.id, user_id=violation.user_id, joined_at=0)
            session.add(member)
            await session.flush()
        action = await enqueue(
            session,
            kind,
            f"{'profile-mute' if profile else 'rule-mute'}:{violation.id}",
            group.id,
            violation.user_id,
            {"violation_id": violation.id, "generation": member.generation, "rule_evidence": rule_evidence},
            1,
            actor_id,
            reason if profile else reason or "正文广告规则命中，删除消息并永久禁言",
        )
        identifiers.append(action.id)
    elif SPAM_REASONS.intersection(violation.reasons) and not violation.sender_chat:
        if member is None:
            member = Member(group_id=group.id, user_id=violation.user_id, joined_at=0)
            session.add(member)
            await session.flush()
        fingerprint = await session.get(Fingerprint, (group.id, violation.user_id, violation.message_id))
        if fingerprint:
            action = await enqueue(session, "spam_mute", f"spam-mute:{violation.id}", group.id, violation.user_id,
                {"violation_id": violation.id, "message_id": violation.message_id, "generation": member.generation,
                 "fingerprint": fingerprint.digest,
                 "spam_settings": {key: group.settings.get(key) for key in SPAM_SETTINGS}},
                1, actor_id, "重复刷屏，删除消息并临时禁言30分钟")
            identifiers.append(action.id)
            if "重复刷屏" in violation.reasons:
                copies = await session.scalars(select(Fingerprint).where(
                    Fingerprint.group_id == group.id, Fingerprint.user_id == violation.user_id,
                    Fingerprint.generation == fingerprint.generation, Fingerprint.digest == fingerprint.digest,
                    Fingerprint.created_at >= fingerprint.created_at - group.settings["repeat_window"],
                    Fingerprint.created_at <= fingerprint.created_at,
                    Fingerprint.message_id != violation.message_id,
                ))
                for copy in copies:
                    if await session.scalar(select(Action.id).where(
                        Action.group_id == group.id, Action.kind == "delete", Action.target_message_id == copy.message_id,
                        Action.state.in_(("pending", "succeeded")),
                    )):
                        continue
                    deletion = await enqueue(session, "delete", f"repeat-delete:{group.id}:{violation.user_id}:{copy.message_id}",
                        group.id, violation.user_id,
                        {"message_id": copy.message_id, "violation_id": violation.id, "spam_fingerprint": copy.digest,
                         "spam_settings": {key: group.settings.get(key) for key in SPAM_SETTINGS}},
                        1, actor_id, "清理同一成员的重复刷屏消息")
                    identifiers.append(deletion.id)
    return identifiers
