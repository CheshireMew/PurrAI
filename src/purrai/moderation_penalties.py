"""Convert recorded moderation facts into durable actions with explicit precedence."""

import secrets

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import (
    Member,
    Verification,
    now,
)
from purrai.rules import CONTACT_REASON
from purrai.verification_cleanup import cleanup
from purrai.verification_state import ACTIVE


async def apply_penalty(session, group, violation, member, actor_id=None, reason=""):
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
    if CONTACT_REASON in violation.reasons and not violation.sender_chat:
        if member is None:
            member = Member(group_id=group.id, user_id=violation.user_id, joined_at=0)
            session.add(member)
            await session.flush()
        action = await enqueue(
            session,
            "contact_kick",
            f"contact-kick:{violation.id}",
            group.id,
            violation.user_id,
            {"violation_id": violation.id, "generation": member.generation},
            0,
            actor_id,
            reason or CONTACT_REASON,
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
                    CONTACT_REASON,
                    now(),
                )
                verification.nonce = secrets.token_hex(4)
                await cleanup(session, verification)
    elif violation.profile_violation and not violation.sender_chat:
        action = await enqueue(
            session,
            "profile_mute",
            f"profile-mute:{violation.id}",
            group.id,
            violation.user_id,
            {"violation_id": violation.id, "generation": member.generation if member else ""},
            1,
            actor_id,
            reason,
        )
        identifiers.append(action.id)
    elif violation.rule_mute and not violation.sender_chat:
        if member is None:
            member = Member(group_id=group.id, user_id=violation.user_id, joined_at=0)
            session.add(member)
            await session.flush()
        action = await enqueue(
            session,
            "rule_mute",
            f"rule-mute:{violation.id}",
            group.id,
            violation.user_id,
            {"violation_id": violation.id, "generation": member.generation},
            1,
            actor_id,
            reason or "正文广告规则命中，删除消息并永久禁言",
        )
        identifiers.append(action.id)
    return identifiers
