"""Durable automatic subscription checks, triggered by events and periodic polling."""

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import Group, Verification, now


async def schedule_check(session, verification, *, immediate=False):
    action = await enqueue(
        session, "subscription_check", f"subscription-check:{verification.id}:{verification.nonce}",
        verification.group_id, verification.user_id, {"verification_id": verification.id}, 1,
    )
    if immediate:
        action.available_at = now()
        # Restored channel access also revives a check that previously lacked permission.
        if action.state == "failed":
            action.state, action.attempts = "pending", 0
    elif action.attempts == 0:
        action.available_at = min(verification.deadline, now() + 15)
    return action


def channel_event(payload):
    event = payload.get("chat_member") or payload.get("my_chat_member") or {}
    return event if event.get("chat", {}).get("type") == "channel" else None


async def subscriptions_for_event(session, event, *, bot_event=False):
    query = select(Verification).join(Group, Group.id == Verification.group_id).where(
        Group.approved.is_(True), Verification.method == "channel",
        Verification.state.in_(("pending", "checking")),
        Verification.message_id.is_not(None),
        Verification.challenge["channel_id"].as_integer() == event["chat"]["id"],
    )
    if not bot_event:
        query = query.where(Verification.user_id == event["new_chat_member"]["user"]["id"])
    return (await session.scalars(query)).all()


async def membership_changed(db, event, *, bot_event=False):
    async with db.transaction() as session:
        for verification in await subscriptions_for_event(session, event, bot_event=bot_event):
            # Events wake the durable check; only a fresh Telegram lookup grants access.
            await schedule_check(session, verification, immediate=True)
