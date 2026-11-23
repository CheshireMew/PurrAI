"""Author-aware checklist/poll additions and exact-revision cleanup of their source message."""
import json

from sqlalchemy import select

from purrai.models import AdvertisementReview, ArchivedMessage, MessageRevision, Meta, now


def event_target(message):
    for field, reference in (("checklist_tasks_added", "checklist_message"), ("poll_option_added", "poll_message")):
        event = message.get(field) or {}
        target = event.get(reference) or {}
        if (isinstance(target.get("message_id"), int) and target["message_id"] > 0
                and target["message_id"] != message.get("message_id")
                and target.get("chat", {}).get("id") == message.get("chat", {}).get("id")):
            return field, target
    return "", None


async def remember_target_edit(session, message, update_id):
    """Retain just target-version evidence when ordinary archive recording is disabled."""
    key = f"content-target:{message['chat']['id']}:{message['message_id']}"
    row = await session.get(Meta, key)
    if row is None and not any(message.get(field) for field in ("checklist", "poll")):
        return
    from purrai.archive import snapshot
    value = {"message": snapshot(message), "update_id": update_id}
    previous = json.loads(row.value) if row else None
    if previous and (previous["message"].get("edit_date", previous["message"]["date"]), previous["update_id"]) > (
            message.get("edit_date", message["date"]), update_id):
        return
    if row is None:
        row = Meta(key=key)
        session.add(row)
    row.value, row.expires_at = json.dumps(value), now() + 47 * 3600


async def event_current(session, message, update_id=0):
    field, target = event_target(message)
    if target is None:
        return True
    candidates = []
    marker = await session.get(Meta, f"content-target:{message['chat']['id']}:{target['message_id']}")
    if marker:
        item = json.loads(marker.value)
        candidates.append((item["message"].get("edit_date", item["message"]["date"]), item["update_id"], item["message"]))
    latest = (await session.execute(select(MessageRevision).join(ArchivedMessage,
        MessageRevision.archive_id == ArchivedMessage.id).where(ArchivedMessage.group_id == message["chat"]["id"],
        ArchivedMessage.message_id == target["message_id"]).order_by(MessageRevision.event_at.desc(),
        MessageRevision.update_id.desc(), MessageRevision.id.desc()).limit(1))).scalar_one_or_none()
    if latest:
        candidates.append((latest.event_at, latest.update_id, latest.payload))
    newer = [item for item in candidates if (item[1] > update_id if item[1] and update_id
             else item[0] >= message.get("edit_date", message["date"]) and item[2].get("edit_date"))]
    if not newer:
        return True
    current = max(newer, key=lambda item: item[:2])[2]
    event = message[field]
    if field == "checklist_tasks_added":
        tasks = {task["id"]: task for task in (current.get("checklist") or {}).get("tasks", [])}
        return all(task["id"] in tasks and all(tasks[task["id"]].get(key) == task.get(key)
                   for key in ("text", "text_entities")) for task in event.get("tasks", []))
    return any(option.get("persistent_id") == event["option_persistent_id"]
               and option.get("text") == event["option_text"]
               and option.get("text_entities") == event.get("option_text_entities")
               for option in (current.get("poll") or {}).get("options", []))


async def action_source(session, action):
    for field in ("message_evidence", "ad_evidence", "contact_evidence"):
        evidence = action.payload.get(field) or {}
        if isinstance(evidence.get("message"), dict):
            return evidence["message"], evidence.get("version", [0, False, action.payload.get("update_id", 0)])[2]
    evidence = action.payload.get("advertisement_evidence") or {}
    if evidence.get("review_id"):
        review = await session.get(AdvertisementReview, evidence["review_id"])
        if review:
            return review.message, review.update_id
    return {}, 0


async def action_event_current(session, action):
    message, update_id = await action_source(session, action)
    return await event_current(session, message, update_id) if message else True


async def queue_related_delete(session, action):
    if action.kind != "delete" or action.state != "succeeded" or action.payload.get("content_event_cleanup"):
        return
    message, _ = await action_source(session, action)
    _, target = event_target(message)
    if target is None:
        return
    from purrai.action_queue import enqueue
    await enqueue(session, "delete", f"content-event-delete:{action.id}:{target['message_id']}", action.group_id,
        action.user_id, {**action.payload, "message_id": target["message_id"], "content_event_cleanup": True},
        priority=action.priority, actor_id=action.actor_id, reason=action.reason)
