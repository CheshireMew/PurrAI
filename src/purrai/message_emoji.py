"""Resolve only custom emoji actually sent in a message to their Telegram sets."""
import json

from sqlalchemy import select

from purrai.models import Meta, now


def identifiers(message):
    entities = (message.get("entities") or []) + (message.get("caption_entities") or [])
    result = {str(item["custom_emoji_id"]) for item in entities
              if item.get("type") == "custom_emoji" and item.get("custom_emoji_id")}
    sticker = message.get("sticker") or {}
    if sticker.get("custom_emoji_id"):
        result.add(str(sticker["custom_emoji_id"]))
    return sorted(result)


def enabled(rules):
    return any(rule["kind"] == "block_emoji_pack" and rule.get("enabled", True) for rule in rules)


async def cached_packs(session, message):
    ids = identifiers(message)
    prefix = "custom-emoji-pack:"
    rows = await session.scalars(select(Meta).where(Meta.key.in_([prefix + value for value in ids]),
                                                   Meta.expires_at > now()))
    found = {row.key[len(prefix):]: json.loads(row.value)["pack"] for row in rows}
    packs = {name for name in found.values() if name}
    # Sticker set metadata belongs to this sent object, never the user's profile or a reply.
    sticker = message.get("sticker") or {}
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
