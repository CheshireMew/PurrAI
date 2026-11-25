"""Persist the expiry of group moderation notices and recover legacy receipts."""

from sqlalchemy import select
from sqlalchemy.orm import aliased

from purrai.action_queue import enqueue
from purrai.models import Action, Group, now
from purrai.schemas import GroupSettings


async def schedule_notice_cleanup(session, action):
    # A published keyboard is a functional message, not an expiring operation receipt.
    if action.kind == "group_menu" and action.payload.get("operation") == "publish":
        return
    delivery = action.payload.get("delivery", {})
    message_id = delivery.get("result", {}).get("message_id")
    if (action.kind not in ("warn", "admin_notify", "group_menu") or not action.group_id or delivery.get("state") != "confirmed"
            or delivery.get("method") != "send_message" or type(message_id) is not int or message_id <= 0):
        return
    if action.payload.get("chat_id", action.group_id) != action.group_id:
        return
    group = await session.get(Group, action.group_id)
    if not group:
        return
    seconds = GroupSettings.model_validate(group.settings).notice_seconds
    key = "cleanup:notice:" + action.id
    if await session.scalar(select(Action.id).where(Action.dedupe_key == key)):
        return
    expires_at = action.payload.get("notice_expires_at") or action.updated_at + seconds
    action.payload = {**action.payload, "notice_expires_at": expires_at}
    deletion = await enqueue(session, "delete", key, action.group_id,
        payload={"message_id": message_id, "chat_id": action.group_id}, priority=5,
        reason=f"群内系统提示发送{seconds}秒后自动清理")
    deletion.available_at = expires_at


class NoticeCleanup:
    def __init__(self, db):
        self.db = db

    async def reconcile(self, limit=100):
        notice, cleanup = aliased(Action), aliased(Action)
        async with self.db.transaction() as session:
            notices = (await session.scalars(select(notice).outerjoin(cleanup,
                cleanup.dedupe_key == "cleanup:notice:" + notice.id).where(
                    notice.kind.in_(("warn", "admin_notify", "group_menu")), notice.group_id.is_not(None),
                    notice.updated_at >= now() - 48 * 3600,
                    notice.payload["delivery"]["state"].as_string() == "confirmed",
                    notice.payload["delivery"]["method"].as_string() == "send_message",
                    notice.payload["delivery"]["result"]["message_id"].as_integer() > 0,
                    (notice.payload["chat_id"].as_integer().is_(None)
                     | (notice.payload["chat_id"].as_integer() == notice.group_id)),
                    cleanup.id.is_(None),
                ).order_by(notice.created_at).limit(limit))).all()
            for action in notices:
                await schedule_notice_cleanup(session, action)
            return len(notices)
