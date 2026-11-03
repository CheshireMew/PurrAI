"""Lifetime, per-group counts of distinct messages actually judged by the model."""

import json

from purrai.action_queue import enqueue
from purrai.models import Member, Meta, Whitelist, now
from purrai.moderation_records import record_violation
from purrai.schemas import GroupSettings

LIMIT = 3


async def advertisement_count(session, group_id, user_id):
    row = await session.get(Meta, f"ai_advertising:{group_id}:{user_id}")
    return len(json.loads(row.value).get("messages", [])) if row else 0


async def record_ai_advertisement(session, group, user_id, message_id, text, user_name, *, protected_ids=()):
    if (type(user_id) is not int or user_id <= 0 or user_id in protected_ids or not group.enabled
            or not GroupSettings.model_validate(group.settings).chat_advertiser_cooldown):
        return
    white = await session.get(Whitelist, (group.id, user_id))
    if white and white.moderation:
        return
    key = f"ai_advertising:{group.id}:{user_id}"
    row = await session.get(Meta, key)
    data = json.loads(row.value) if row else {"messages": []}
    if any(item["id"] == message_id for item in data["messages"]):
        return
    if len(data["messages"]) < LIMIT:
        data["messages"].append({"id": message_id, "at": now(), "excerpt": text[:600]})
    if row:
        row.value = json.dumps(data, ensure_ascii=False)
    else:
        session.add(Meta(key=key, value=json.dumps(data, ensure_ascii=False)))
    if len(data["messages"]) < LIMIT:
        return
    member = await session.get(Member, (group.id, user_id))
    if member is None:
        member = Member(group_id=group.id, user_id=user_id, joined_at=0)
        session.add(member)
        await session.flush()
    violation, _ = await record_violation(session, group_id=group.id, user_id=user_id,
        message_id=message_id, reasons=["AI累计3次判定广告，永久禁言"],
        excerpt=text, sender_name=user_name, rule_mute=True)
    await enqueue(session, "ai_ad_mute", f"ai-ad-mute:{group.id}:{user_id}:{member.generation}:{message_id}",
        group.id, user_id, {"violation_id": violation.id, "generation": member.generation,
            "user_name": user_name, "model_message_ids": [item["id"] for item in data["messages"]]},
        priority=1, reason="AI对3条不同消息实际判定广告，累计次数不设时间窗口")
