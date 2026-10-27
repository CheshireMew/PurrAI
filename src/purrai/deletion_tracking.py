"""Update archive and chat projections when a deletion task changes state."""

from purrai.archive import record_deletion
from purrai.chat_messages import invalidate_message


async def track_deletion(session, action):
    if action.kind != "delete" or action.payload.get("chat_id", action.group_id) != action.group_id:
        return
    if action.state != "cancelled":
        await invalidate_message(session, action.group_id, action.payload.get("message_id"))
    await record_deletion(session, action)
