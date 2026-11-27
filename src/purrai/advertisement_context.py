"""Prepare current message evidence and model context inside checked review stages."""

from sqlalchemy import select

from purrai.advertisement_content import context_sensitive, include_number_readings
from purrai.advertisement_store import policy
from purrai.message_buttons import inline_buttons
from purrai.message_identity import profile_name, message_sender, sender_chat
from purrai.message_parts import message_text
from purrai.models import AdvertisementReview
from purrai.policy_work import policy_work
from purrai.rules import ai_keyword_hints
from purrai.sender_profile import biography
from purrai.shared_policy import effective_rules
from purrai.text_matching import message_content, normalize


async def prepare_review(session, rows):
    gid = rows[0].group_id
    request, hints = ([], {})
    rules = await effective_rules(session, gid)
    cfg, _ = await policy(session, gid)
    for row in rows:
        saved = await session.get(AdvertisementReview, row.id)
        message = dict(saved.message)
        body = message_text(message)
        name = profile_name(message_sender(message))
        hints[row.id] = await policy_work(session, ai_keyword_hints, body, rules, name, biography(message))
        previous = await recent_messages(session, row, cfg)
        context = [
            {
                "id": item.id,
                "revision": item.revision,
                "message_id": item.message_id,
                "text": normalize(message_text(item.message))[:800],
            }
            for item in reversed(previous)
            if not item.message.get("_profile_event")
        ]
        message["_ad_context"] = context
        saved.message = message
        request.append(review_message(row, message, body, name, context, hints[row.id]))
    return (request, hints, rules)


async def recent_messages(session, row, cfg):
    previous = (
        (
            await session.scalars(
                select(AdvertisementReview)
                .where(
                    AdvertisementReview.group_id == row.group_id,
                    AdvertisementReview.user_id == row.user_id,
                    AdvertisementReview.message_id != row.message_id,
                    AdvertisementReview.state != "superseded",
                    (AdvertisementReview.event_at < row.event_at)
                    | (AdvertisementReview.event_at == row.event_at)
                    & (AdvertisementReview.message_id < row.message_id),
                    AdvertisementReview.event_at >= row.event_at - cfg.context_window_seconds,
                )
                .order_by(AdvertisementReview.event_at.desc(), AdvertisementReview.update_id.desc())
                .limit(cfg.context_messages)
            )
        ).all()
        if cfg.context_messages
        else []
    )
    return previous


def review_message(row, message, body, name, context, hints):
    reference = message.get("reply_to_message") or {}
    return include_number_readings(
        {
            "id": row.id,
            "name": normalize(name),
            "text": normalize(body),
            "sender_type": "channel"
            if sender_chat(message)
            else "bot"
            if message_sender(message).get("is_bot")
            else "user",
            "links": message_content(message)[1],
            "buttons": inline_buttons(message),
            "bio": normalize(biography(message)),
            "bio_status": (message.get("_sender_profile") or {}).get("status", "unavailable"),
            "recent_messages": [{"message_id": item["message_id"], "text": item["text"]} for item in context],
            "keyword_hints": hints,
            "reference": {
                "name": profile_name(message_sender(reference)),
                "text": message_text(reference)[:800],
            },
            "quote": message.get("quote"),
            "external_reply": message.get("external_reply"),
            "quoted": context_sensitive(message),
            "image_unavailable": bool(
                message.get("photo") or message.get("video") or message.get("document")
            ),
        }
    )
