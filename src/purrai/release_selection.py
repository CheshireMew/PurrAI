"""Freeze the exact members reviewed before a bulk release."""

import hashlib
import json

from sqlalchemy import select

from purrai.errors import ManagementError
from purrai.models import Member, Verification
from purrai.verification_state import ACTIVE


def selection_snapshot(rows):
    # A natural pending -> releasing transition is material: ask the admin to
    # review again instead of silently superseding a later member decision.
    values = sorted((row.id, row.generation, row.state, row.nonce) for row in rows)
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


async def selected_verifications(session, group_id, identifiers=None, *, user_id=None, after=0, before=0, state=""):
    if before and after > before:
        raise ManagementError(422, "开始时间不能晚于结束时间")
    query = select(Verification).join(Member, (Member.group_id == Verification.group_id)
        & (Member.user_id == Verification.user_id) & (Member.generation == Verification.generation)).where(
            Verification.group_id == group_id, Verification.state.in_(ACTIVE))
    if identifiers is not None:
        query = query.where(Verification.id.in_(identifiers))
    if user_id is not None:
        query = query.where(Verification.user_id == user_id)
    if state:
        query = query.where(Verification.state == state)
    if after:
        query = query.where(Verification.created_at >= after)
    if before:
        query = query.where(Verification.created_at <= before)
    rows = (await session.scalars(query.order_by(Verification.created_at, Verification.id).limit(1001))).all()
    if len(rows) > 1000:
        raise ManagementError(422, "本次待放行超过 1000 人，请按用户或时间缩小筛选范围后分批核对")
    if identifiers is not None and {row.id for row in rows} != set(identifiers):
        raise ManagementError(409, "所选验证或入群批次已变化，请刷新名单重新核对")
    return rows
