"""Administration operations for group admin."""

from sqlalchemy import select
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from purrai.shared_policy import initial_settings
from purrai.action_queue import enqueue
from purrai.errors import ManagementError
from purrai.models import (
    Action,
    Group,
    Verification,
    now,
)
from purrai.serialization import row_dict
from purrai.telegram import is_present
from purrai.verification_cleanup import cleanup
from purrai.verification_state import ACTIVE


class GroupAdministration:
    def __init__(self, context):
        self.context = context

    async def group_access(self, actor, group_id, allow):
        await self.context.access.principal(actor, root=True)
        return await self.set_group_access(group_id, allow, actor)

    async def admit_group(self, actor, group_id):
        await self.context.access.principal(actor, root=True)
        if group_id >= 0 or group_id < -(2**63):
            raise ManagementError(422, "请填写 Telegram 群的负数数字 ID")
        try:
            chat = await self.context.tg.call("get_chat", chat_id=group_id)
            if chat.get("id") != group_id or chat.get("type") not in ("group", "supergroup"):
                raise ManagementError(422, "只能接入群组，请填写群的数字 ID")
            member = await self.context.tg.call(
                "get_chat_member", chat_id=group_id, user_id=self.context.tg.user_id
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            raise ManagementError(422, "无法访问此群，请核对群 ID 并先将机器人加入群") from exc
        except ManagementError:
            raise
        except Exception as exc:
            raise ManagementError(503, "暂时无法向 Telegram 核实群信息，请稍后重试") from exc
        if not is_present(member):
            raise ManagementError(422, "机器人尚未加入此群，请先将机器人加入群")
        return await self.set_group_access(group_id, True, actor, chat=chat)

    async def set_group_access(self, group_id, allow, actor=None, *, chat=None):
        """The authenticated owner and local CLI share this one mutation."""
        from purrai.models import ChatTurn

        async with self.context.db.transaction() as session:
            group = await session.get(Group, group_id)
            if group_id >= 0:
                raise ManagementError(422, "请填写 Telegram 群的负数 ID")
            if group is None and allow:
                group = Group(
                    id=group_id,
                    title=str(group_id),
                    type="supergroup",
                    approved=False,
                    settings=await initial_settings(session),
                )
                session.add(group)
                await session.flush()
            if not group:
                raise ManagementError(404, "群不存在，请先将机器人加入该群")
            if chat:
                group.title, group.type = chat.get("title", str(group_id))[:128], chat["type"]
            group.approved = allow
            if not allow:
                group.enabled, group.status = False, "unapproved"
                group.settings = {**group.settings, "chat_enabled": False}
                group.setting_overrides = sorted(set(group.setting_overrides or []) | {"chat_enabled"})
                for action in await session.scalars(
                    select(Action).where(Action.group_id == group_id, Action.state == "pending")
                ):
                    if not action.dedupe_key.startswith("cleanup:"):
                        action.state, action.error = "cancelled", "所有者已撤销此群接入"
                for turn in await session.scalars(
                    select(ChatTurn).where(
                        ChatTurn.group_id == group_id, ChatTurn.state.in_(("pending", "generating"))
                    )
                ):
                    turn.state, turn.error = "skipped", "所有者已撤销此群接入"
                for verification in await session.scalars(
                    select(Verification).where(
                        Verification.group_id == group_id, Verification.state.in_(ACTIVE)
                    )
                ):
                    verification.state, verification.error = "cancelled", "所有者已撤销此群接入"
                    await cleanup(session, verification)
            elif group.status == "unapproved":
                group.status = "pending"
            return await self.context.record(
                session,
                actor,
                group_id,
                "group_access",
                "批准群接入" if allow else "撤销群接入",
                row_dict(group),
            )

    async def permissions(self, actor, group_id, activate=False):
        group = await self.context.access.authorize(actor, group_id, write=True)
        if activate and group.type != "supergroup":
            raise ManagementError(422, "成员禁言需要 Telegram 超级群，请先升级群组")
        try:
            permissions = await self.context.access.check_permissions(
                group_id, group.settings.get("join_requests", False)
            )
        except Exception as exc:
            raise ManagementError(503, "无法检查机器人群权限，请确认机器人已加入群并能连接 Telegram") from exc
        async with self.context.access.group_transaction(group_id) as session:
            group = await session.get(Group, group_id)
            group.permissions = permissions
            if activate and permissions["ok"]:
                if not group.enabled:
                    group.activated_at = now()
                group.enabled = True
                await self.context.record(session, actor, group_id, "activate", "启用群保护", {"ok": True})
            group.status = (
                "active"
                if permissions["ok"] and group.enabled
                else ("pending" if permissions["ok"] else "error")
            )
        if activate and not permissions["ok"]:
            raise ManagementError(422, "机器人缺少权限：" + "、".join(permissions["missing"]))
        return {"ok": permissions["ok"], "permissions": permissions, **permissions}

    async def join_link(self, actor, group_id, request_id):
        group = await self.context.access.authorize(actor, group_id, write=True)
        if not group.enabled or not group.settings.get("join_requests"):
            raise ManagementError(409, "请先启用群保护和申请入群验证")
        await self.context.access.require_join_right(group_id)
        async with self.context.access.group_transaction(group_id) as session:
            row = await enqueue(
                session,
                "join_link",
                f"join-link:{actor}:{request_id}",
                group_id,
                priority=5,
                actor_id=actor,
                reason="生成需要审核的入群链接",
            )
            return row_dict(row)
