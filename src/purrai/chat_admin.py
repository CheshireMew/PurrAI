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
