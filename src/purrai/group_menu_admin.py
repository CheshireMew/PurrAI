"""Group-scoped, versioned and idempotent menu administration."""
from purrai.action_queue import enqueue
from purrai.admin_context import AdminContext
from purrai.errors import ManagementError
from purrai.group_menu import default_menu, menu_document
from purrai.models import GroupMenu


class GroupMenuAdmin:
    def __init__(self, context: AdminContext):
        self.context = context

    async def get(self, actor, group_id):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            return await menu_document(session, group_id)

    async def change(self, actor, group_id, body, request_id, *, save=False):
        await self.context.access.authorize(actor, group_id, write=True)
        intent = {"operation": "menu_save" if save else "menu_operate", "group_id": group_id, **body.model_dump()}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            row = await session.get(GroupMenu, group_id)
            if body.version != (row.version if row else 0):
                raise ManagementError(409, "菜单设置已更新，请刷新后再操作")
            if row is None:
                row = GroupMenu(group_id=group_id, config=default_menu(), version=0)
                session.add(row)
            if save:
                row.config, row.version = body.config.model_dump(), row.version + 1
                reason = "保存底部菜单"
            else:
                operation = body.operation
                reason = "更新群底部菜单" if operation == "publish" else "清除群底部菜单"
                action = await enqueue(session, "group_menu", f"group-menu:{actor}:{request_id}", group_id,
                    actor_id=actor, reason=reason, payload={"operation": body.operation, "version": row.version})
                row.last_action_id = action.id
                # Operations also advance the version so concurrent administrators cannot overwrite a newer request.
                row.version += 1
                action.payload = {**action.payload, "version": row.version}
            await session.flush()
            return await self.context.record(session, actor, group_id, "group_menu_settings", reason,
                await menu_document(session, group_id), request_id, intent)
