"""Verification transitions shared by permanent member restrictions."""

import secrets

from sqlalchemy import select

from purrai.models import Verification, now
from purrai.verification_cleanup import cleanup
from purrai.verification_state import ACTIVE

PENALTIES = {
    "permanent_ban": ("永久封禁", "已踢出并禁止重新加入"),
    "permanent_mute": ("永久禁言", "已永久禁言"),
}


async def active_verifications(session, member):
    return (await session.scalars(select(Verification).where(
        Verification.group_id == member.group_id, Verification.user_id == member.user_id,
        Verification.generation == member.generation, Verification.state.in_(ACTIVE),
    ))).all()


def begin_penalty(verifications, reason, penalty):
    pending, _ = PENALTIES[penalty]
    for verification in verifications:
        verification.state = "failing"
        verification.nonce = secrets.token_hex(4)
        verification.error = reason + "；等待" + pending + "完成"


async def finish_penalty(session, verifications, reason, penalty):
    _, completed = PENALTIES[penalty]
    for verification in verifications:
        verification.state, verification.failure_action, verification.updated_at = "failed", penalty, now()
        verification.error = reason + "，" + completed
        await cleanup(session, verification)
