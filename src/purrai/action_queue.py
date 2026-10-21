"""Durable action creation and deduplication, shared by application services."""

from sqlalchemy import select

from purrai.archive import deletion_result
from purrai.models import Action, Verification, now, uid


async def enqueue(
    session,
    kind: str,
    key: str,
    group_id=None,
    user_id=None,
    payload=None,
    priority=10,
    actor_id=None,
    reason="",
    batch_id=None,
) -> Action:
    existing = await session.scalar(select(Action).where(Action.dedupe_key == key))
    if existing:
        return existing
    payload = dict(payload or {})
    if payload.get("verification_id"):
        verification = await session.get(Verification, payload["verification_id"])
        if verification:
            payload.setdefault("expected_nonce", verification.nonce)
    item = Action(
        id=uid(),
        kind=kind,
        dedupe_key=key,
        batch_id=batch_id,
        group_id=group_id,
        user_id=user_id,
        payload=payload,
        priority=priority,
        actor_id=actor_id,
        reason=reason,
        state="pending",
        attempts=0,
        available_at=now(),
        error="",
    )
    session.add(item)
    await session.flush()
    if kind == "delete":
        await deletion_result(session, item)
    return item


async def audit(session, kind: str, actor_id: int, group_id=None, reason="", payload=None):
    item = await enqueue(
        session, kind, uid(), group_id=group_id, actor_id=actor_id, reason=reason, payload=payload
    )
    item.state = "succeeded"
    return item
