"""Configurable rolling counts of distinct messages actually judged by the model."""

import json

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import ArchivedMessage, ChatMessage, Group, Member, Meta, Whitelist, now
from purrai.moderation_records import record_rule_hits, record_violation
from purrai.message_identity import profile_name
from purrai.rules import ai_keyword_rules
from purrai.schemas import GroupSettings
from purrai.shared_policy import effective_rules

# Compatibility exports for callers inspecting the initial policy, never used
# to decide live moderation.
LIMIT = GroupSettings().ai_ad_limit
WINDOW = GroupSettings().ai_ad_window_hours * 3600


def recent_judgments(data, cfg):
    cutoff = now() - cfg.ai_ad_window_hours * 3600
    return [item for item in data.get("messages", []) if item["at"] > cutoff]


async def advertisement_count(session, group_id, user_id):
    group = await session.get(Group, group_id)
    cfg = GroupSettings.model_validate(group.settings if group else {})
    row = await session.get(Meta, f"ai_advertising:{group_id}:{user_id}")
    return len(recent_judgments(json.loads(row.value), cfg)) if row else 0


async def cumulative_penalty_applies(session, action, group):
    if not group or not group.enabled:
        return False
    cfg = GroupSettings.model_validate(group.settings)
    return bool((cfg.chat_advertiser_cooldown or "advertisement_evidence" in action.payload)
                and cfg.ai_ad_penalty == "permanent_mute"
                and await advertisement_count(session, group.id, action.user_id) >= cfg.ai_ad_limit)


async def keyword_penalty_applies(session, action, group):
    """Recheck saved model evidence and current editable rules before either action."""
    evidence = action.payload.get("ai_keyword_evidence") or {}
    independent = "advertisement_evidence" in action.payload
    if not group or not group.enabled or not independent and not GroupSettings.model_validate(group.settings).chat_advertiser_cooldown:
        return False
    if independent:
        from purrai.advertisement_store import evidence_applies
        if not await evidence_applies(session, action, group):
            return False
    # A biography recheck may release the writer. Read rules and counts afterwards.
    identifier, text = evidence.get("message_id"), evidence.get("text", "")
    verdicts = await session.get(Meta, f"ai_advertising:{group.id}:{action.user_id}")
    if not verdicts or not any(item["id"] == identifier and item["excerpt"] == text[:600]
                               for item in recent_judgments(json.loads(verdicts.value), GroupSettings.model_validate(group.settings))):
        return False
    rules = await effective_rules(session, group.id)
    sender_name = evidence.get("sender_name", "")
    if not any(rule.get("id") in evidence.get("rule_ids", [])
               and ("rule_versions" not in evidence or evidence["rule_versions"].get(rule["id"]) == rule.get("version"))
               for rule in ai_keyword_rules(text, rules, sender_name, evidence.get("bio", ""))):
        return False
    return independent or await unchanged_message(session, action, evidence)


async def deletion_applies(session, action, group):
    """A queued actual verdict may delete only its unchanged source message."""
    evidence = action.payload.get("ai_ad_evidence") or {}
    if "criteria_version" in evidence:
        from purrai.advertisement_store import criteria_version
        if evidence["criteria_version"] != criteria_version(await effective_rules(session, action.group_id)):
            return False
    return bool(group and group.enabled and GroupSettings.model_validate(group.settings).chat_advertiser_cooldown
                and now() - evidence.get("judged_at", 0) < GroupSettings.model_validate(group.settings).ai_ad_window_hours * 3600
                and await unchanged_message(session, action, evidence))


async def unchanged_message(session, action, evidence):
    identifier, text = evidence.get("message_id"), evidence.get("text", "")
    sender_name = evidence.get("sender_name", "")
    # Either recording path may observe an edit first. Never apply an old model
    # verdict to new text, even when that new text contains the same keyword.
    recorded = False
    for model in (ChatMessage, ArchivedMessage):
        row = await session.scalar(select(model).where(model.group_id == action.group_id, model.message_id == identifier))
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


async def record_ai_advertisement(session, group, user_id, message_id, text, user_name, *, protected_ids=(), independent_evidence=None, criteria=None, sender_bio="", profile_event=False):
    if (type(user_id) is not int or user_id <= 0 or user_id in protected_ids or not group.enabled
            or not independent_evidence and not GroupSettings.model_validate(group.settings).chat_advertiser_cooldown):
        return
    white = await session.get(Whitelist, (group.id, user_id))
    if white and white.moderation:
        return
    key = f"ai_advertising:{group.id}:{user_id}"
    cfg = GroupSettings.model_validate(group.settings)
    row = await session.get(Meta, key)
    data = json.loads(row.value) if row else {"messages": []}
    data["messages"] = recent_judgments(data, cfg)
    seen = any(item["id"] == message_id for item in data["messages"])
    if seen and not independent_evidence:
        return
    data["messages"] = ([item for item in data["messages"] if item["id"] != message_id]
                        + [{"id": message_id, "at": now(), "excerpt": text[:600]}])[-1000:]
    if row:
        row.value = json.dumps(data, ensure_ascii=False)
    else:
        session.add(Meta(key=key, value=json.dumps(data, ensure_ascii=False)))
    matched = ai_keyword_rules(text, await effective_rules(session, group.id), user_name, sender_bio)
    mute = bool(matched or cfg.ai_ad_penalty == "permanent_mute" and len(data["messages"]) >= cfg.ai_ad_limit)
    member = await session.get(Member, (group.id, user_id))
    if member is None:
        member = Member(group_id=group.id, user_id=user_id, joined_at=0)
        session.add(member)
        await session.flush()
    reasons = ([f"AI确认广告且命中关键词：{rule['value']}" for rule in matched]
               if matched else [f"AI确认广告：最近{cfg.ai_ad_window_hours}小时累计{cfg.ai_ad_limit}次，永久禁言"] if mute else ["AI确认广告，" + ("已记录简介判定" if profile_event else "删除消息")])
    violation, changed = await record_violation(session, group_id=group.id, user_id=user_id,
        message_id=message_id, reasons=reasons,
        excerpt=sender_bio if profile_event else text, sender_name=user_name, rule_mute=mute, message_violation=not profile_event)
    if not changed and not independent_evidence:
        return
    evidence = {}
    if matched:
        evidence = {"ai_keyword_evidence": {"message_id": message_id, "text": text, "sender_name": user_name,
                                           "bio": sender_bio,
                                           "rule_ids": [rule["id"] for rule in matched],
                                           "rule_versions": {rule["id"]: rule.get("version") for rule in matched}}}
        await record_rule_hits(session, group.id, message_id, evidence["ai_keyword_evidence"]["rule_ids"])
    delete_evidence = {"ai_ad_evidence": {"message_id": message_id, "text": text,
                                         "sender_name": user_name, "judged_at": now()}}
    if criteria is not None:
        delete_evidence["ai_ad_evidence"]["criteria_version"] = criteria
        evidence.update(delete_evidence)
    suffix = ":" + independent_evidence["review_id"] if independent_evidence else ""
    if independent_evidence:
        delete_evidence = {"advertisement_evidence": independent_evidence}
        evidence.update(delete_evidence)
    if not profile_event:
        await enqueue(session, "delete", f"ai-ad-delete:{group.id}:{message_id}{suffix}", group.id, user_id,
            {"message_id": message_id, "violation_id": violation.id, "generation": member.generation, **delete_evidence},
            priority=0, reason="AI确认广告，删除消息")
    if not mute:
        return
    await enqueue(session, "ai_ad_mute", f"ai-ad-mute:{group.id}:{user_id}:{member.generation}:{message_id}{suffix}",
        group.id, user_id, {"violation_id": violation.id, "generation": member.generation,
            "user_name": user_name, "model_message_ids": [item["id"] for item in data["messages"]], **evidence},
        priority=1, reason="AI确认广告且命中启用的关键词" if matched else f"AI最近{cfg.ai_ad_window_hours}小时对{cfg.ai_ad_limit}条不同消息实际判定广告")
