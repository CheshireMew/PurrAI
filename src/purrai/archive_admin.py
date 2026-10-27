"""Administration operations for archive admin."""

from purrai.action_queue import enqueue
from purrai.errors import ManagementError
from purrai.models import (
    now,
)
from purrai.serialization import row_dict


class ArchiveAdministration:
    def __init__(self, context):
        self.context = context

    async def archived_messages(self, actor, group_id, filters, page_number=1, size=20):
        from purrai.archive import page

        await self.context.access.authorize(actor, group_id)
        return await page(self.context.db, group_id, filters, page_number, size, settings=self.context.settings)

    async def archived_message(self, actor, group_id, identifier):
        from purrai.archive import detail

        await self.context.access.authorize(actor, group_id)
        result = await detail(self.context.db, group_id, identifier)
        if result is None:
            raise ManagementError(404, "消息归档不存在")
        return result

    async def export_stream(self, actor, group_id, filters):
        from purrai.archive import export_jsonl

        await self.context.access.authorize(actor, group_id)
        return export_jsonl(self.context.db, group_id, filters)

    async def archive_export(self, actor, group_id, filters, request_id):
        await self.context.access.authorize(actor, group_id)
        async with self.context.access.group_transaction(group_id) as session:
            task = await enqueue(
                session,
                "archive_export",
                f"archive-export:{actor}:{request_id}",
                group_id,
                actor_id=actor,
                reason="导出群消息归档",
                payload={"filters": filters.model_dump(), "cutoff": now()},
            )
            return row_dict(task)
