"""Group-scoped points policy administration shared by management adapters."""

from sqlalchemy import func, literal_column, select

from purrai.admin_context import AdminContext
from purrai.advertisement_schemas import LOCAL_REVIEW_WINDOW_SECONDS
from purrai.advertisement_store import policy as advertisement_policy
from purrai.errors import ManagementError
from purrai.models import Action, PointsPolicy
from purrai.points import PointsLedger
from purrai.points_policy import get_policy, policy_document


class PointsAdministration:
    def __init__(self, context: AdminContext):
        self.context = context

    async def settings(self, actor, group_id):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            return await policy_document(session, group_id)

    async def page(self, actor, group_id, **filters):
        await self.context.access.authorize(actor, group_id)
        result = await PointsLedger(self.context.db).page(group_id, **filters)
        async with self.context.db.sessions() as session:
            config = await get_policy(session, group_id)
            review, _ = await advertisement_policy(session, group_id)
            count, oldest = (await session.execute(select(func.count(), func.min(Action.created_at)).where(
                Action.group_id == group_id, Action.state == "pending", Action.kind == "points_message",
                func.json_extract(Action.payload, literal_column("'$.operation'")) == "chat"))).one()
            mode = ("disabled" if not config.enabled or not config.chat_points or not config.chat_daily_limit
                    else "direct" if not review.enabled else "model" if self.context.settings.chat_ready else "local")
            result["settlement"] = {"mode": mode, "pending_messages": count, "oldest_at": oldest,
                                    "local_window_hours": LOCAL_REVIEW_WINDOW_SECONDS // 3600}
        return result

    async def save_settings(self, actor, group_id, body, request_id):
        await self.context.access.authorize(actor, group_id, write=True)
        intent = {"group_id": group_id, "operation": "points_policy", **body.model_dump()}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            row = await session.get(PointsPolicy, group_id)
            if body.version != (row.version if row else 0):
                raise ManagementError(409, "积分设置已更新，请刷新后再保存")
            if row is None:
                row = PointsPolicy(group_id=group_id, version=0)
                session.add(row)
            row.config, row.version = body.config.model_dump(), row.version + 1
            await session.flush()
            return await self.context.record(
                session,
                actor,
                group_id,
                "points_policy",
                "保存积分设置",
                await policy_document(session, group_id),
                request_id,
                intent,
            )
