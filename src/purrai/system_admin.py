"""Administration operations for system admin."""

from purrai.action_queue import enqueue
from purrai.errors import ManagementError
from purrai.models import (
    Action,
    Inbox,
    Meta,
    now,
)


class SystemAdministration:
    def __init__(self, context):
        self.context = context

    async def track(self, actor, group_id, chat_id, action_ids, request_id):
        await self.context.access.authorize(actor, group_id)
        if not action_ids:
            return
        async with self.context.access.group_transaction(group_id) as session:
            await enqueue(
                session,
                "admin_notify",
                f"notify:{actor}:{request_id}",
                group_id,
                payload={"chat_id": chat_id, "action_ids": action_ids},
                priority=20,
            )

    async def backup(self, actor, request_id=""):
        await self.context.access.principal(actor, root=True)
        async with self.context.db.sessions() as session:
            previous = await self.context.previous(session, actor, request_id)
            if previous is not None:
                return previous
        path = await self.context.db.backup(self.context.settings.backup_dir)
        async with self.context.db.transaction() as session:
            row = await session.get(Meta, "last_backup")
            if row:
                row.value = str(path)
            else:
                session.add(Meta(key="last_backup", value=str(path)))
            return await self.context.record(
                session,
                actor,
                None,
                "backup",
                "生成一致性数据库备份",
                {"file": path.name, "created_at": now()},
                request_id,
            )

    async def retry(self, actor, identifier, event=False, request_id=""):
        await self.context.access.principal(actor, root=True)
        async with self.context.db.transaction() as session:
            previous = await self.context.previous(session, actor, request_id)
            if previous is not None:
                return previous
            row = await session.get(Inbox if event else Action, identifier)
            if not row or row.state != "failed":
                raise ManagementError(409, "此事件当前不能重试" if event else "此任务当前不能重试")
            row.state, row.attempts, row.available_at, row.error = "pending", 0, now(), ""
            return await self.context.record(
                session,
                actor,
                None if event else row.group_id,
                "retry_event" if event else "retry",
                "重试失败事件" if event else "重试失败任务",
                {"ok": True, "event_id" if event else "action_id": identifier},
                request_id,
            )
