"""Persist the expiry of group moderation notices and recover legacy receipts."""

from sqlalchemy import select
from sqlalchemy.orm import aliased

from purrai.action_queue import enqueue
from purrai.models import Action, now

NOTICE_SECONDS = 180


async def schedule_notice_cleanup(session, action):
    delivery = action.payload.get("delivery", {})
    message_id = delivery.get("result", {}).get("message_id")
    if (action.kind != "warn" or not action.group_id or delivery.get("state") != "confirmed"
            or delivery.get("method") != "send_message" or type(message_id) is not int or message_id <= 0):
        return
    key = "cleanup:notice:" + action.id
    if await session.scalar(select(Action.id).where(Action.dedupe_key == key)):
        return
    expires_at = action.payload.get("notice_expires_at") or (
        action.updated_at if action.state == "succeeded" else now()) + NOTICE_SECONDS
    action.payload = {**action.payload, "notice_expires_at": expires_at}
    deletion = await enqueue(session, "delete", key, action.group_id,
        payload={"message_id": message_id, "chat_id": action.group_id}, priority=5,
        reason="群内系统提示发送180秒后自动清理")
    deletion.available_at = expires_at


class NoticeCleanup:
    def __init__(self, db):
        self.db = db

    async def reconcile(self, limit=100):
        notice, cleanup = aliased(Action), aliased(Action)
        async with self.db.transaction() as session:
            notices = (await session.scalars(select(notice).outerjoin(cleanup,
                cleanup.dedupe_key == "cleanup:notice:" + notice.id).where(
                    notice.kind == "warn", notice.state == "succeeded", notice.group_id.is_not(None),
                    notice.updated_at >= now() - 48 * 3600,
                    notice.payload["delivery"]["state"].as_string() == "confirmed",
                    notice.payload["delivery"]["method"].as_string() == "send_message",
                    notice.payload["delivery"]["result"]["message_id"].as_integer() > 0,
                    cleanup.id.is_(None),
                ).order_by(notice.created_at).limit(limit))).all()
            for action in notices:
                await schedule_notice_cleanup(session, action)
            return len(notices)
