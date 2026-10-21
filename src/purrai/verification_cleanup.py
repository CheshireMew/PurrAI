"""Durable cleanup of exact CAPTCHA prompts."""

from sqlalchemy import String, or_, select

from purrai.action_queue import enqueue
from purrai.models import (
    Action,
    Verification,
    now,
)


async def cleanup(session, verification):
    if verification.message_id:
        await enqueue(
            session,
            "delete",
            f"cleanup:{verification.id}:{verification.message_id}",
            verification.group_id,
            verification.user_id,
            {
                "message_id": verification.message_id,
                "chat_id": verification.prompt_chat_id or verification.group_id,
            },
            priority=5,
        )


async def observe_callback_prompt(session, verification, query, nonce, bot_id):
    """Recover a lost send receipt or remove an exact stale prompt seen in a callback."""
    message = query.get("message") or {}
    message_id = message.get("message_id")
    chat_id = message.get("chat", {}).get("id")
    if (
        type(message_id) is not int or message_id <= 0
        or chat_id != (verification.prompt_chat_id or verification.group_id)
        or message.get("from", {}).get("id", bot_id) != bot_id
    ):
        return
    if (
        verification.deadline <= now()
        or verification.state not in ("muting", "pending")
        or verification.nonce != nonce
        or verification.message_id and verification.message_id != message_id
    ):
        await enqueue(
            session, "delete", f"cleanup:callback:{verification.id}:{message_id}",
            verification.group_id, verification.user_id,
            {"chat_id": chat_id, "message_id": message_id}, priority=5,
        )
    elif verification.state == "pending" and not verification.message_id and query["from"]["id"] == verification.user_id:
        action = await session.scalar(select(Action).where(
            Action.dedupe_key == f"challenge:{verification.id}:{nonce}"
        ))
        if action and action.payload.get("challenge_delivery") == "uncertain":
            verification.message_id, verification.error, verification.updated_at = message_id, "", now()
            action.payload = {**action.payload, "challenge_delivery": "recovered"}
            action.state, action.error, action.updated_at = "succeeded", "", now()


class VerificationCleanup:
    def __init__(self, db):
        self.db = db

    async def reconcile_verification_cleanup(self, limit=100):
        """Recover missing cleanup intents without repeating any member action."""
        key = "cleanup:" + Verification.id + ":" + Verification.message_id.cast(String)
        async with self.db.transaction() as session:
            rows = (
                await session.scalars(
                    select(Verification)
                    .outerjoin(Action, Action.dedupe_key == key)
                    .where(
                        Verification.message_id > 0,
                        or_(Verification.deadline <= now(), Verification.state.not_in(("muting", "pending"))),
                        Action.id.is_(None),
                    )
                    .order_by(Verification.created_at, Verification.id)
                    .limit(limit)
                )
            ).all()
            for verification in rows:
                await cleanup(session, verification)
            return len(rows)
