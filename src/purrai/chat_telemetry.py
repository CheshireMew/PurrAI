"""Bounded operational timings and daily usage; no chat body or model output."""

import json

from purrai.models import Meta, now


async def observation(session, group_id, message_id, topic_id, state, reason):
    from sqlalchemy import delete, select
    prefix = f"chat-observed:{group_id}:"
    key = prefix + str(message_id)
    row = await session.get(Meta, key)
    data = {"message_id": message_id, "topic_id": topic_id, "state": state, "reason": reason, "created_at": now()}
    if row:
        row.value, row.expires_at = json.dumps(data), now() + 3 * 86400
    else:
        session.add(Meta(key=key, value=json.dumps(data), expires_at=now() + 3 * 86400))
    await session.flush()
    excess = select(Meta.key).where(Meta.key.startswith(prefix)).order_by(Meta.expires_at.desc()).offset(500)
    await session.execute(delete(Meta).where(Meta.key.in_(excess)).execution_options(synchronize_session=False))


async def timing(session, turn_id, **values):
    if turn_id is None:
        return
    key = f"chat-timing:{turn_id}"
    row = await session.get(Meta, key)
    data = json.loads(row.value) if row else {}
    data.update(values)
    if row:
        row.value = json.dumps(data)
        row.expires_at = now() + 3 * 86400
    else:
        session.add(Meta(key=key, value=json.dumps(data), expires_at=now() + 3 * 86400))


async def usage(session, day, group_id, topic_id, *, calls=0, tokens=0):
    key = f"chat-usage:{day}:{group_id}:{topic_id}"
    row = await session.get(Meta, key)
    data = json.loads(row.value) if row else {"day": day, "group_id": group_id, "topic_id": topic_id, "calls": 0, "tokens": 0}
    data.update(calls=data["calls"] + calls, tokens=max(0, data["tokens"] + tokens))
    if row:
        row.value = json.dumps(data)
    else:
        session.add(Meta(key=key, value=json.dumps(data), expires_at=now() + 90 * 86400))
