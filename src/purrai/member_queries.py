"""Member history and separately verified current Telegram membership."""

from sqlalchemy import func, select

from purrai.errors import ManagementError
from purrai.models import Action, Member, Verification, Violation, Whitelist, now
from purrai.serialization import row_dict


class MemberQueries:
    def __init__(self, context, queries):
        self.context, self.queries = context, queries

    async def detail(self, actor, group_id, user_id):
        await self.context.access.authorize(actor, group_id)
        if user_id <= 0:
            raise ManagementError(422, "请填写成员的正数数字 ID")
        current, error = None, ""
        try:
            current = await self.context.tg.call("get_chat_member", chat_id=group_id, user_id=user_id, request_timeout=3)
        except Exception:
            error = "暂时无法核实 Telegram 当前状态；下面的历史记录仍可查看"
        async with self.context.db.sessions() as session:
            member = await session.get(Member, (group_id, user_id))
            white = await session.get(Whitelist, (group_id, user_id))
            result = {"user_id": user_id, "member": row_dict(member) if member else None,
                      "whitelist": row_dict(white) if white else None, "current": current,
                      "current_error": error, "checked_at": now() if current is not None else None}
            for name, model in (("verifications", Verification), ("violations", Violation), ("actions", Action)):
                where = (model.group_id == group_id, model.user_id == user_id)
                rows = (await session.scalars(select(model).where(*where).order_by(model.created_at.desc()).limit(20))).all()
                result[name] = {"items": await self.queries.project(session, model, rows),
                                "total": await session.scalar(select(func.count()).select_from(model).where(*where))}
            return result
