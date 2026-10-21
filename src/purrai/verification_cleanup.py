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
