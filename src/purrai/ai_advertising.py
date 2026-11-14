"""Rolling three-day counts of distinct messages actually judged by the model."""

import json

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import ArchivedMessage, ChatMessage, Member, Meta, Whitelist, now
from purrai.moderation_records import record_rule_hits, record_violation
from purrai.message_identity import profile_name
from purrai.rules import ai_keyword_rules
from purrai.schemas import GroupSettings
from purrai.shared_policy import effective_rules

LIMIT = 3
WINDOW = 3 * 86400


def recent_judgments(data):
    cutoff = now() - WINDOW
    return [item for item in data.get("messages", []) if item["at"] > cutoff]


async def advertisement_count(session, group_id, user_id):
    row = await session.get(Meta, f"ai_advertising:{group_id}:{user_id}")
    return len(recent_judgments(json.loads(row.value))) if row else 0


async def keyword_penalty_applies(session, action, group):
    """Recheck saved model evidence and current editable rules before either action."""
    evidence = action.payload.get("ai_keyword_evidence") or {}
    if not group or not group.enabled or not GroupSettings.model_validate(group.settings).chat_advertiser_cooldown:
        return False
    identifier, text = evidence.get("message_id"), evidence.get("text", "")
    verdicts = await session.get(Meta, f"ai_advertising:{group.id}:{action.user_id}")
    if not verdicts or not any(item["id"] == identifier and item["excerpt"] == text[:600]
                               for item in recent_judgments(json.loads(verdicts.value))):
        return False
    rules = await effective_rules(session, group.id)
    sender_name = evidence.get("sender_name", "")
    if not any(rule.get("id") in evidence.get("rule_ids", []) for rule in ai_keyword_rules(text, rules, sender_name)):
        return False
    # Either recording path may observe an edit first. Never apply an old model
    # verdict to new text, even when that new text contains the same keyword.
    recorded = False
    for model in (ChatMessage, ArchivedMessage):
        row = await session.scalar(select(model).where(model.group_id == group.id, model.message_id == identifier))
        if row is not None:
            recorded = True
            current_text = row.text or (row.caption if isinstance(row, ArchivedMessage) else "")
            if row.sender_id != action.user_id or current_text[:1600] != text:
                return False
            current_name = (profile_name({"first_name": row.sender_name, "username": row.username})
                            if isinstance(row, ArchivedMessage) else row.sender_name)
            if "sender_name" in evidence and current_name != sender_name:
                return False
    return recorded


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
    data["messages"] = recent_judgments(data)
    if any(item["id"] == message_id for item in data["messages"]):
        return
    data["messages"] = (data["messages"] + [{"id": message_id, "at": now(), "excerpt": text[:600]}])[-LIMIT:]
    if row:
        row.value = json.dumps(data, ensure_ascii=False)
    else:
        session.add(Meta(key=key, value=json.dumps(data, ensure_ascii=False)))
    matched = ai_keyword_rules(text, await effective_rules(session, group.id), user_name)
    if len(data["messages"]) < LIMIT and not matched:
        return
    member = await session.get(Member, (group.id, user_id))
    if member is None:
        member = Member(group_id=group.id, user_id=user_id, joined_at=0)
        session.add(member)
        await session.flush()
    reasons = ([f"AI确认广告且命中关键词：{rule['value']}" for rule in matched]
               if matched else ["AI最近3天累计3次判定广告，永久禁言"])
    violation, changed = await record_violation(session, group_id=group.id, user_id=user_id,
        message_id=message_id, reasons=reasons,
        excerpt=text, sender_name=user_name, rule_mute=True, message_violation=bool(matched))
    if not changed:
        return
    evidence = {}
    if matched:
        evidence = {"ai_keyword_evidence": {"message_id": message_id, "text": text, "sender_name": user_name,
                                           "rule_ids": [rule["id"] for rule in matched]}}
        await record_rule_hits(session, group.id, message_id, evidence["ai_keyword_evidence"]["rule_ids"])
        await enqueue(session, "delete", f"ai-ad-delete:{group.id}:{message_id}", group.id, user_id,
            {"message_id": message_id, "violation_id": violation.id, "generation": member.generation, **evidence},
            priority=0, reason="AI确认广告且命中启用的关键词")
    await enqueue(session, "ai_ad_mute", f"ai-ad-mute:{group.id}:{user_id}:{member.generation}:{message_id}",
        group.id, user_id, {"violation_id": violation.id, "generation": member.generation,
            "user_name": user_name, "model_message_ids": [item["id"] for item in data["messages"]], **evidence},
        priority=1, reason="AI确认广告且命中启用的关键词" if matched else "AI最近72小时对3条不同消息实际判定广告")
