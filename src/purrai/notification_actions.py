"""Message deletion, notifications and archive delivery."""

from aiogram.exceptions import TelegramBadRequest

from purrai.action_delivery import deliver_once
from purrai.admin_labels import OPERATIONS, STATE
from purrai.media_gate import media_gate_exemption
from purrai.models import (
    Action,
    now,
)


class NotificationActions:
    def __init__(self, db, transport):
        self.db, self.action_call = db, transport.call

    async def delete_action_message(self, session, action):
        try:
            await self.action_call(
                session,
                action,
                "delete_message",
                chat_id=action.payload.get("chat_id", action.group_id),
                message_id=action.payload["message_id"],
            )
        except TelegramBadRequest as exc:
            if "message to delete not found" not in str(exc).lower():
                raise
            action.payload = {**action.payload, "already_absent": True}

    async def delete(self, session, action, group, verification, member):
        if await media_gate_exemption(session, action, group, member):
            action.state, action.error = "cancelled", "成员已免除新成员发图门槛"
            return
        await self.delete_action_message(session, action)

    async def notify(self, session, action, group, verification, member):
        sources = [await session.get(Action, identifier) for identifier in action.payload["action_ids"]]
        if any(item and item.state == "pending" for item in sources):
            action.available_at = now() + 2
            return False
        lines = [f"PurrAI · {group.title if group else '管理'} · 执行结果"]
        for source in sources[:20]:
            if source:
                target = f" · 用户 {source.user_id}" if source.user_id is not None else ""
                lines.append(
                    f"{OPERATIONS.get(source.kind, source.kind)}{target}："
                    f"{STATE.get(source.state, source.state)}"
                    + (f"\n原因：{source.error}" if source.error else "")
                )
                if source.kind == "join_link" and source.state == "succeeded":
                    lines.append(source.payload.get("invite_link", ""))
        if len(sources) > 20:
            lines.append(f"共 {len(sources)} 项；其余结果可在机器人操作记录查看。")
        await deliver_once(
            self.action_call, session, action, "send_message", chat_id=action.payload["chat_id"], text="\n".join(lines)[:4000]
        )

    async def join_link(self, session, action, group, verification, member):
        response = await deliver_once(
            self.action_call, session,
            action,
            "create_chat_invite_link",
            chat_id=group.id,
            name="PurrAI 验证入群",
            creates_join_request=True,
        )
        action.payload = {**action.payload, "invite_link": response["invite_link"]}
        from purrai.group_link_identity import remember_group_link

        await remember_group_link(session, group.id, response["invite_link"])

    async def export_archive(self, session, action, group, verification, member):
        from purrai.archive import export_document
        from purrai.schemas import ArchiveFilter

        filters = ArchiveFilter.model_validate(action.payload["filters"])
        content, count = await session.info["action_execution"].external(
            lambda: export_document(self.db, group.id, filters, action.payload["cutoff"])
        )
        await deliver_once(
            self.action_call, session,
            action,
            "send_document",
            chat_id=action.actor_id,
            document=bytes(content),
            filename=f"purrai-{group.id}-messages.jsonl",
            caption=f"{group.title} · {count} 条归档消息，包含编辑版本与处置结果。媒体仅保存标识和元数据。",
        )
        action.payload = {**action.payload, "exported_count": count}

    async def warn(self, session, action, group, verification, member):
        await deliver_once(self.action_call, session, action, "send_message", chat_id=group.id, text=action.payload["text"])
