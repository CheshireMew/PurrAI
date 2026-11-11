"""Convert recorded moderation facts into durable actions with explicit precedence."""

import secrets

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import (
    Member,
    Verification,
    now,
)
from purrai.rules import CONTACT_REASON, CONTACT_REASONS, message_content
from purrai.advertising_patterns import is_ban_reason
from purrai.verification_cleanup import cleanup
from purrai.verification_state import ACTIVE


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
            {"message_id": violation.message_id, "violation_id": violation.id},
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
    return identifiers
