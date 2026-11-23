"""Choose one owner for a message's advertising verdict and optional chat reply."""
from sqlalchemy import select

from purrai.models import AdvertisementReview, ChatTurn, now


def shared_review(review, message_id):
    return bool(review and review.message_id == message_id and review.state in ("pending", "requesting")
                and review.message.get("_chat_turn_id"))


async def finish_routing(db, message):
    async with db.transaction() as session:
        reviews = (await session.scalars(select(AdvertisementReview).where(
            AdvertisementReview.group_id == message["chat"]["id"],
            AdvertisementReview.message_id == message["message_id"], AdvertisementReview.state == "pending",
        ))).all()
        for review in reviews:
            if not review.message.get("_chat_routing"):
                continue
            payload = {key: value for key, value in review.message.items() if key != "_chat_routing"}
            turn = await session.scalar(select(ChatTurn).where(
                ChatTurn.group_id == review.group_id, ChatTurn.message_id == review.message_id,
                ChatTurn.state == "pending", ChatTurn.review_only.is_(False),
            ))
            if turn:
                payload["_chat_turn_id"] = turn.id
            review.message = payload


async def recover_routing(session, *, startup_before=None):
    rows = (await session.scalars(select(AdvertisementReview).where(
        AdvertisementReview.state.in_(("pending", "requesting")),
        AdvertisementReview.message["_chat_turn_id"].as_integer().is_not(None)
        | AdvertisementReview.message["_chat_routing"].as_boolean().is_(True),
    ))).all()
    for review in rows:
        if review.message.get("_chat_routing"):
            if startup_before is None or review.created_at >= startup_before:
                continue
        turn = await session.get(ChatTurn, review.message.get("_chat_turn_id")) if review.message.get("_chat_turn_id") else None
        if turn and turn.state in ("pending", "generating", "sending"):
            continue
        review.message = {key: value for key, value in review.message.items() if key not in ("_chat_routing", "_chat_turn_id")}
        review.state, review.available_at, review.local_after = "pending", now(), 0
