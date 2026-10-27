"""Durable ownership of explicit member decisions across retries and restarts."""

from sqlalchemy import select

from purrai.models import Action, Meta, now

MEMBER_DECISIONS = (
    "verify_mute", "challenge", "release", "ban", "manual_ban", "manual_mute", "unmute", "unban",
)


def decision_key(group_id, user_id):
    return f"member-decision:{group_id}:{user_id}"


def explicit_decision(action):
    return bool(action.actor_id is not None and action.group_id and action.user_id
                and action.kind in MEMBER_DECISIONS)


async def remember_decision(session, action):
    if not explicit_decision(action):
        return
    key = decision_key(action.group_id, action.user_id)
    current = await session.get(Meta, key)
    if current:
        current.value = action.id
    else:
        session.add(Meta(key=key, value=action.id))
    # A newer human decision replaces earlier human work, including a retry
    # whose response was lost. Failed receipts remain available for inspection;
    # current_decision also prevents manually retrying those obsolete receipts.
    for old in await session.scalars(select(Action).where(
        Action.group_id == action.group_id, Action.user_id == action.user_id,
        Action.actor_id.is_not(None), Action.kind.in_(MEMBER_DECISIONS),
        Action.state == "pending", Action.id != action.id,
    )):
        old.state, old.error, old.updated_at = "cancelled", "管理员已作出新的成员处理决定", now()
    verification_id = action.payload.get("verification_id")
    if verification_id:
        for old in await session.scalars(select(Action).where(
            Action.group_id == action.group_id, Action.user_id == action.user_id,
            Action.state == "pending", Action.id != action.id,
            Action.kind.in_((*MEMBER_DECISIONS, "verification_failure", "progress")),
            Action.payload["verification_id"].as_string() == verification_id,
        )):
            old.state, old.error, old.updated_at = "cancelled", "管理员已更新此验证的处理决定", now()


async def current_decision(session, action):
    if not explicit_decision(action):
        return True
    current = await session.get(Meta, decision_key(action.group_id, action.user_id), populate_existing=True)
    if current:
        # SQLAlchemy's identity map uses weak references. Retain this decision
        # until the session ends so external() can compare it after every I/O.
        session.info["member_decision"] = current
    return current is None or current.value == action.id
