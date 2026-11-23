"""Durable cleanup of exact CAPTCHA prompts."""

from sqlalchemy import String, and_, literal_column, or_, select
from sqlalchemy.orm import aliased

from purrai.action_queue import enqueue
from purrai.models import (
    Action,
    Group,
    Verification,
    now,
)
from purrai.verification_state import confirm_prompt, queued_answer


async def cleanup(session, verification):
    message_ids = {verification.message_id} if verification.message_id else set()
    anchors = await session.scalars(select(Action.payload["challenge_message_id"].as_integer()).where(
        Action.kind == "challenge", Action.group_id == verification.group_id,
        Action.payload["verification_id"].as_string() == verification.id,
        Action.payload["challenge_message_id"].as_integer() > 0,
    ))
    message_ids.update(anchors)
    for message_id in message_ids:
        await enqueue(
            session,
            "delete",
            f"cleanup:{verification.id}:{message_id}",
            verification.group_id,
            verification.user_id,
            {
                "message_id": message_id,
                "chat_id": verification.prompt_chat_id or verification.group_id,
            },
            priority=5,
        )


async def observe_callback_prompt(session, verification, query, nonce, bot_id, *, received_at=None):
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
    if (query.get("data", "").endswith(":load") and verification.state == "pending"
            and verification.nonce == nonce and query["from"]["id"] == verification.user_id
            and not verification.message_id):
        action = await session.scalar(select(Action).where(
            Action.kind == "challenge", Action.group_id == verification.group_id,
            Action.payload["verification_id"].as_string() == verification.id,
            Action.payload["challenge_nonce"].as_string() == nonce,
        ).order_by(Action.created_at.desc()).limit(1))
        if action and action.state in ("pending", "failed"):
            anchor = action.payload.get("challenge_message_id")
            delivery = action.payload.get("delivery", {})
            if anchor == message_id or not anchor and delivery.get("state") in ("sending", "uncertain"):
                action.payload = {**action.payload, "challenge_message_id": message_id,
                    "delivery": {"state": "confirmed", "method": "send_message", "result": {"message_id": message_id}}}
                action.state, action.error, action.available_at = "pending", "", now()
                return "正在重新加载验证图片，请稍候。"
    if (
        verification.message_id and verification.deadline <= (received_at if received_at is not None else now())
        or verification.state not in ("muting", "pending", "checking")
        or verification.nonce != nonce
        or verification.message_id and verification.message_id != message_id
    ):
        await enqueue(
            session, "delete", f"cleanup:callback:{verification.id}:{message_id}",
            verification.group_id, verification.user_id,
            {"chat_id": chat_id, "message_id": message_id}, priority=5,
        )
    elif verification.state == "pending" and not verification.message_id and query["from"]["id"] == verification.user_id:
        # Request deduplication keys differ across automatic and manual entry
        # points. The verification and the actual send nonce own the receipt.
        action = await session.scalar(select(Action).where(
            Action.kind == "challenge", Action.group_id == verification.group_id,
            Action.user_id == verification.user_id,
            Action.payload["verification_id"].as_string() == verification.id,
        ).order_by(Action.created_at.desc(), literal_column("actions.rowid").desc()).limit(1))
        if (action and action.payload.get("challenge_delivery") in ("uncertain", "sending")
                and action.payload.get("challenge_nonce", nonce) == nonce):
            # Old persisted actions have no challenge_nonce. A current, bot-owned
            # callback carrying the current nonce remains valid upgrade evidence.
            confirm_prompt(verification, await session.get(Group, verification.group_id), message_id)
            action.payload = {**{key: value for key, value in action.payload.items() if key != "challenge_image"},
                              "challenge_delivery": "recovered"}
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
                        or_(Verification.state.not_in(("muting", "pending", "checking")),
                            and_(Verification.state.in_(("muting", "pending")), Verification.method != "channel",
                                 Verification.deadline <= now())),
                        Action.id.is_(None),
                    )
                    .order_by(Verification.created_at, Verification.id)
                    .limit(limit)
                )
            ).all()
            processed = 0
            for verification in rows:
                if verification.state in ("muting", "pending") and await queued_answer(session, verification):
                    continue
                await cleanup(session, verification)
                processed += 1
            # Uploads may have a known placeholder even before the picture and
            # its answer deadline are confirmed. Ended verifications still own it.
            cleaned = aliased(Action)
            anchor_key = "cleanup:" + Verification.id + ":" + Action.payload["challenge_message_id"].as_integer().cast(String)
            anchors = (await session.execute(select(Verification, Action).join(
                Action, Action.payload["verification_id"].as_string() == Verification.id,
            ).outerjoin(cleaned, cleaned.dedupe_key == anchor_key).where(
                Verification.state.not_in(("muting", "pending", "checking")), Action.kind == "challenge",
                Action.payload["challenge_message_id"].as_integer() > 0, cleaned.id.is_(None),
            ).order_by(Verification.created_at, Verification.id).limit(max(0, limit - processed)))).all()
            for verification, action in anchors:
                await cleanup(session, verification)
                processed += 1
            return processed
