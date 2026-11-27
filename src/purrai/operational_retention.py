"""Retain necessary moderation evidence without keeping every runtime copy forever."""

import json

from sqlalchemy import func, literal_column, or_, select

from purrai.advertisement_store import wake_chat_reward
from purrai.command_text import normalized_command
from purrai.models import Action, AdvertisementReview, AdvertisementSample, Meta, Violation, now
from purrai.review_storage import BYTES_KEY, LIMITED_KEY, payload_size, retained_message


async def storage_status(session, settings):
    value = await session.scalar(select(Meta.value).where(Meta.key == BYTES_KEY))
    used = int(value) if value is not None else int(await session.scalar(select(
        func.coalesce(func.sum(payload_size(AdvertisementReview.message)), 0))))
    limited = await session.scalar(select(Meta.value).where(Meta.key == LIMITED_KEY))
    return {"bytes": used, "limit_bytes": settings.review_payload_max_mb * 1048576,
            "limited": json.loads(limited) if limited else None,
            "payload_retention_hours": settings.review_payload_retention_hours,
            "history_retention_days": settings.review_history_retention_days}


async def prune_operational_state(session, settings, limit=250):
    status = await storage_status(session, settings)
    excess = max(0, status["bytes"] - status["limit_bytes"])
    cutoff = now() - settings.review_payload_retention_hours * 3600
    raw = literal_column("json_extract(message, '$._retained') IS NOT 1")
    condition = AdvertisementReview.event_at < cutoff
    if excess:
        condition = or_(condition, AdvertisementReview.event_at <= now())
    rows = list(await session.scalars(select(AdvertisementReview).where(raw, condition)
        .order_by(AdvertisementReview.event_at, AdvertisementReview.id).limit(limit)))
    for row in rows:
        if row.event_at >= cutoff and excess <= 0:
            continue
        excess -= len(json.dumps(row.message).encode())
        row.message = retained_message(row)
        if row.state in ("pending", "requesting"):
            row.state, row.error = "cancelled", "审核原文已到保留期限或暂存额度不足，停止旧审核"
            await wake_chat_reward(session, row)
    # Completed ordinary-message jobs need their outcome, not a second raw
    # Telegram message. Financial entries and manual-operation audit stay intact.
    jobs = list(await session.scalars(select(Action).where(Action.kind == literal_column("'points_message'"),
        Action.state.in_(("succeeded", "cancelled", "reviewed", "failed", "pending")),
        Action.created_at < cutoff,
        func.json_type(Action.payload, literal_column("'$.message'")).is_not(None)).order_by(Action.created_at, Action.id).limit(limit)))
    for job in jobs:
        source = job.payload["message"]
        receipt = {}
        if "reply" in job.payload:
            receipt = {"reply_source": {key: source[key] for key in (
                "message_id", "message_thread_id", "is_topic_message") if key in source}}
            if job.payload.get("menu_operation"):
                receipt["reply_source"]["text"] = normalized_command(source.get("text", ""))
        elif job.state == "pending":
            job.state, job.error = "cancelled", "原消息已超过积分处理期限，停止旧任务"
        job.payload = {key: value for key, value in job.payload.items() if key != "message"}
        job.payload = {**job.payload, "source_expired": True, **receipt}
    history_cutoff = now() - settings.review_history_retention_days * 86400
    historical = select(AdvertisementReview).where(AdvertisementReview.created_at < history_cutoff,
        AdvertisementReview.event_at < cutoff,
        ~select(AdvertisementSample.id).where(AdvertisementSample.group_id == AdvertisementReview.group_id,
            AdvertisementSample.source_key == "review:" + AdvertisementReview.id).exists(),
        ~select(Violation.id).where(Violation.group_id == AdvertisementReview.group_id,
            Violation.message_id == AdvertisementReview.message_id).exists(),
        ~select(Action.id).where(Action.advertisement_review_id == AdvertisementReview.id).exists(),
    ).order_by(AdvertisementReview.created_at, AdvertisementReview.id).limit(limit)
    old = list(await session.scalars(historical))
    for row in old:
        await session.delete(row)
    return max(len(rows), len(jobs), len(old))
