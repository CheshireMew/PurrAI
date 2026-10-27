"""Record a deletion outcome and reconcile any dependent chat replies."""

from purrai.deletion_tracking import track_deletion
from purrai.chat_retraction import retract_replies, retraction_result


async def deletion_result(session, action):
    await track_deletion(session, action)
    if action.kind != "delete" or action.payload.get("chat_id", action.group_id) != action.group_id:
        return
    if action.state == "succeeded":
        await retract_replies(session, action.group_id, action.payload.get("message_id"))
    if action.payload.get("chat_turn_id"):
        await retraction_result(session, action.payload["chat_turn_id"])
