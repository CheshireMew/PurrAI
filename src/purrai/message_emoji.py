"""Resolve only custom emoji actually sent in a message to their Telegram sets."""
import json

from sqlalchemy import select

from purrai.models import Meta, now
from purrai.message_parts import content_parts, message_entities, message_media


def metrics(message):
    total, covered, count = 0, 0, 0
    for part in content_parts(message):
        size = len(part.text.encode("utf-16-le")) // 2
        spans = {(e.get("offset", -1), e.get("offset", -1) + e.get("length", 0)) for e in part.entities
            if e.get("type") == "custom_emoji" and e.get("custom_emoji_id")
            and type(e.get("offset")) is int and type(e.get("length")) is int
            and 0 <= e["offset"] < e["offset"] + e["length"] <= size}
        count += len(spans)
        offset = 0
        for char in part.text:
            end = offset + len(char.encode("utf-16-le")) // 2
            if not char.isspace():
                total += 1
                covered += any(start <= offset and end <= stop for start, stop in spans)
            offset = end
    return {"text_length": total, "custom_emoji_count": count, "custom_emoji_percent": 100 * covered / total if total else 0}


def identifiers(message):
    entities = message_entities(message)
    result = {str(item["custom_emoji_id"]) for item in entities
              if item.get("type") == "custom_emoji" and item.get("custom_emoji_id")}
    for sticker in message_media(message):
        if sticker.get("custom_emoji_id"):
            result.add(str(sticker["custom_emoji_id"]))
    return sorted(result)


def enabled(rules):
    from purrai.business_rules import document_for
    return (any(rule["kind"] == "block_emoji_pack" and rule.get("enabled", True) for rule in rules)
        or any(stage["enabled"] and any(rule["enabled"] and rule.get("learn_emoji_packs", False) for rule in stage["rules"])
               for stage in document_for(rules)["stages"]))


async def cached_packs(session, message):
    ids = identifiers(message)
    prefix = "custom-emoji-pack:"
    rows = await session.scalars(select(Meta).where(Meta.key.in_([prefix + value for value in ids]),
                                                   Meta.expires_at > now()))
    found = {row.key[len(prefix):]: json.loads(row.value)["pack"] for row in rows}
    packs = {name for name in found.values() if name}
    # Sticker set metadata belongs to this sent object, never the user's profile or a reply.
    for sticker in message_media(message):
        if sticker.get("set_name"):
            packs.add(sticker["set_name"].lower())
    return sorted(packs), [value for value in ids if value not in found]


async def resolve(db, telegram, message):
    async with db.sessions() as session:
        packs, missing = await cached_packs(session, message)
    resolved = {}
    for offset in range(0, len(missing), 200):
        if telegram is None:
            raise ConnectionError("消息表情来源尚未核实，等待重试")
        batch = missing[offset:offset + 200]
        response = await telegram.call("get_custom_emoji_stickers", custom_emoji_ids=batch, request_timeout=3)
        if not isinstance(response, list):
            raise ConnectionError("Telegram 未返回有效的消息表情来源")
        for sticker in response:
            identifier = str(sticker.get("custom_emoji_id", ""))
            if identifier in batch:
                resolved[identifier] = sticker.get("set_name", "").lower()
        if any(value not in resolved for value in batch):
            raise ConnectionError("Telegram 漏答部分消息表情来源，等待重试")
    if resolved:
        async with db.transaction() as session:
            for identifier, pack in resolved.items():
                key = "custom-emoji-pack:" + identifier
                row = await session.get(Meta, key)
                if row is None:
                    row = Meta(key=key)
                    session.add(row)
                row.value = json.dumps({"pack": pack})
                row.expires_at = now() + 7 * 86400
    return sorted(set(packs) | {value for value in resolved.values() if value})


async def remember_packs(session, group, violation, evidence):
    """Learn only sent packs from a configured verdict, with reversible source records."""
    from purrai.action_queue import enqueue
    from purrai.models import PromotionSource, SharedPolicy, uid
    from purrai.shared_policy import effective_rules, policy
    from purrai.business_rules import BusinessEngine
    from purrai.message_parts import message_text

    message = (evidence or {}).get("message")
    if not message or not any(item.get("learn_emoji_packs") for item in (violation.reason_details or {}).values()):
        return []
    rules = await effective_rules(session, group.id)
    engine = BusinessEngine(rules)
    if "message_structure" not in engine.stages:
        return []
    hits = [hit for hit in engine.run("message_structure", message_text(message), metrics=metrics(message))
            if hit.reason.learn_emoji_packs]
    if not hits:
        return []
    packs, missing = await cached_packs(session, message)
    if missing:
        raise ConnectionError("消息表情来源尚未核实，等待自动学习")
    local = {rule["value"].lower() for rule in rules if rule["kind"] == "block_emoji_pack" and not rule.get("inherited_from")}
    packs = [pack for pack in packs if pack not in local]
    if not packs:
        return []
    current = await policy(session)
    if current not in session:
        current = SharedPolicy(id=1, settings=current.settings, rules=current.rules, business_rules=current.business_rules)
        session.add(current)
    existing = {rule["value"].lower(): rule for rule in current.rules if rule["kind"] == "block_emoji_pack"}
    rank = {"delete": 0, "mute": 1, "ban": 2}
    penalty = {"delete": "delete", "mute": "permanent_mute", "ban": "ban"}[max(hits, key=lambda hit: rank[hit.action]).action]
    added = [{"id": uid(), "kind": "block_emoji_pack", "value": pack, "mode": "exact", "scope": "message",
        "context_value": "", "penalty": penalty, "enabled": True, "origin": "learned", "learned_at": now()}
        for pack in packs if pack not in existing]
    if added:
        current.rules = [*current.rules, *added]
    indexed = {**existing, **{rule["value"]: rule for rule in added}}
    for pack in packs:
        identifier = indexed[pack]["id"]
        if not await session.get(PromotionSource, (identifier, group.id, violation.message_id)):
            session.add(PromotionSource(rule_id=identifier, group_id=group.id, message_id=violation.message_id,
                violation_id=violation.id))
    if added:
        action = await enqueue(session, "rule", f"learn-emoji-packs:{group.id}:{violation.message_id}", group.id,
            payload={"result": {"emoji_packs": [rule["value"] for rule in added], "source_group_id": group.id,
                "source_message_id": violation.message_id}}, reason="广告消息的表情包自动加入黑名单")
        action.state = "succeeded"
    return [rule["value"] for rule in added]
