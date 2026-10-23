"""Remember links attributed to this group by trusted Telegram updates."""

import json

from sqlalchemy import or_, select

from purrai.models import Inbox, Meta
from purrai.telegram_links import parse_link


async def remember_group_link(session, group_id, value):
    link = parse_link(value)
    if not link or link.kind != "invite":
        return
    row = await session.get(Meta, f"group-links:{group_id}")
    keys = json.loads(row.value) if row else []
    if link.key not in keys:
        keys = [*keys, link.key][-500:]
        if row:
            row.value = json.dumps(keys)
        else:
            session.add(Meta(key=f"group-links:{group_id}", value=json.dumps(keys)))


async def remember_update_links(session, payload):
    for kind in ("chat_member", "my_chat_member", "chat_join_request"):
        event = payload.get(kind, {})
        chat = event.get("chat", {})
        if chat.get("id"):
            invite = event.get("invite_link") or {}
            if invite.get("invite_link"):
                await remember_group_link(session, chat["id"], invite["invite_link"])


async def own_link_keys(session, group_id):
    row = await session.get(Meta, f"group-links:{group_id}")
    marker = await session.get(Meta, f"group-links-backfilled:{group_id}")
    if marker:
        return json.loads(row.value) if row else []
    # Existing installations already retain the original trusted member updates.
    # Backfill once so links created before this feature need not be re-entered.
    for payload in await session.scalars(select(Inbox.payload).where(or_(
        Inbox.payload["chat_member"]["chat"]["id"].as_integer() == group_id,
        Inbox.payload["chat_join_request"]["chat"]["id"].as_integer() == group_id,
    )).order_by(Inbox.id.desc()).limit(1000)):
        await remember_update_links(session, payload)
    session.add(Meta(key=f"group-links-backfilled:{group_id}", value="1"))
    row = await session.get(Meta, f"group-links:{group_id}")
    if row is None:
        session.add(Meta(key=f"group-links:{group_id}", value="[]"))
        return []
    return json.loads(row.value)
