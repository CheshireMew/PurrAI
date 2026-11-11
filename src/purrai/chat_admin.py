"""Explicitly bound chat administration."""

from purrai.errors import ManagementError
from purrai.models import (
    Meta,
    now,
)


class ChatAdministration:
    def __init__(self, context, chat=None):
        self.context, self.chat = context, chat

    async def chat_status(self, actor, group_id):
        await self.context.access.authorize(actor, group_id)
        if self.chat is None:
            raise ManagementError(503, "聊天服务尚未初始化")
        return await self.chat.status(group_id)

    async def diagnostics(self, actor, group_id, *, topic_id=None, state="", page_number=1, size=20):
        import json
        from sqlalchemy import func, select
        from purrai.models import ChatTurn

        await self.context.access.authorize(actor, group_id)
        filters = [ChatTurn.group_id == group_id]
        if topic_id is not None:
            filters.append(ChatTurn.topic_id == topic_id)
        if state:
            filters.append(ChatTurn.state == state)
        async with self.context.db.sessions() as session:
            total = await session.scalar(select(func.count()).select_from(ChatTurn).where(*filters))
            rows = (await session.scalars(select(ChatTurn).where(*filters).order_by(ChatTurn.id.desc())
                .offset((page_number - 1) * size).limit(size))).all()
            items = []
            for row in rows:
                marker = await session.get(Meta, f"chat-timing:{row.id}")
                items.append({"id": row.id, "message_id": row.message_id, "topic_id": row.topic_id,
                    "state": row.state, "direct": row.direct, "attempts": row.attempts, "reason": row.error,
                    "created_at": row.created_at, "available_at": row.available_at,
                    "timing": json.loads(marker.value) if marker else None})
            history = []
            for marker in await session.scalars(select(Meta).where(Meta.key.startswith("chat-usage:"),
                                                   Meta.expires_at > now()).order_by(Meta.key.desc())):
                data = json.loads(marker.value)
                if data["group_id"] == group_id and (topic_id is None or topic_id == data["topic_id"]):
                    history.append(data)
            decisions = []
            for marker in await session.scalars(select(Meta).where(
                Meta.key.startswith(f"chat-observed:{group_id}:"), Meta.expires_at > now())
                .order_by(Meta.expires_at.desc()).limit(500)):
                decision = json.loads(marker.value)
                if topic_id is None or decision["topic_id"] == topic_id:
                    decisions.append(decision)
            return {"items": items, "total": total, "page": page_number, "size": size, "usage": history[:500],
                    "decisions": decisions[:50],
                    "turn_retention_hours": 48, "usage_retention_days": 90}

    async def reset_chat(self, actor, group_id, request_id):
        from sqlalchemy import update

        from purrai.models import ChatTurn

        await self.context.access.authorize(actor, group_id, write=True)
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id)
            if previous is not None:
                return previous
            key = f"chat_reset:{group_id}"
            row = await session.get(Meta, key)
            if row:
                row.value = str(now())
            else:
                session.add(Meta(key=key, value=str(now())))
            await session.execute(
                update(ChatTurn)
                .where(
                    ChatTurn.group_id == group_id,
                    ChatTurn.state.in_(("pending", "generating")),
                )
                .values(state="skipped")
            )
            return await self.context.record(
                session, actor, group_id, "settings", "重新开始群聊天", {}, request_id
            )
