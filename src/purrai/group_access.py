"""Current group access and Telegram role checks shared by commands and workers."""

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import select

from purrai.errors import ManagementError
from purrai.models import Group, now
from purrai.serialization import row_dict
from purrai.telegram import is_admin


class GroupAccess:
    def __init__(self, db, telegram, settings):
        self.db, self.tg, self.settings = db, telegram, settings

    @staticmethod
    async def current_group(session, group_id):
        group = await session.get(Group, group_id, populate_existing=True)
        if not group:
            raise ManagementError(404, "群不存在，请先将机器人加入群")
        if not group.approved:
            raise ManagementError(403, "此群尚未获准接入，请联系机器人所有者批准")
        return group

    @asynccontextmanager
    async def group_transaction(self, group_id):
        # Telegram authorization/permission requests happen outside the write
        # lock. Recheck admission under the same lock as every group mutation.
        async with self.db.transaction() as session:
            await self.current_group(session, group_id)
            yield session

    async def principal(self, actor: int, root=False) -> bool:
        owner = actor in self.settings.superadmin_ids
        if not owner and root:
            raise ManagementError(403, "此操作仅限超级管理员")
        if not owner and not await self.groups(actor):
            raise ManagementError(403, "你目前不是机器人已加入群的管理员，请先在 Telegram 群中设置管理员身份")
        return owner

    async def authorize(self, actor: int, group_id: int, write=False, root=False) -> Group:
        owner = actor in self.settings.superadmin_ids
        if root:
            await self.principal(actor, root=True)
        async with self.db.sessions() as session:
            group = await self.current_group(session, group_id)
        if not owner:
            try:
                member = await self.tg.call(
                    "get_chat_member", chat_id=group_id, user_id=actor, request_timeout=3
                )
            except Exception as exc:
                raise ManagementError(503, "暂时无法核实 Telegram 群管理员身份") from exc
            if not is_admin(member):
                raise ManagementError(403, "你不是此群的 Telegram 管理员，无法管理此群")
            async with self.db.sessions() as session:
                group = await self.current_group(session, group_id)
        return group

    async def groups(self, actor):
        async with self.db.sessions() as session:
            rows = (await session.scalars(select(Group).order_by(Group.title, Group.id))).all()
        if actor in self.settings.superadmin_ids:
            return [row_dict(row) for row in rows]
        semaphore = asyncio.Semaphore(4)

        async def check(row):
            try:
                async with semaphore:
                    member = await self.tg.call(
                        "get_chat_member", chat_id=row.id, user_id=actor, request_timeout=3
                    )
            except Exception:
                return None
            return is_admin(member)

        approved_rows = [row for row in rows if row.approved]
        roles = await asyncio.gather(*(check(row) for row in approved_rows))
        result = [row_dict(row) for row, administrator in zip(approved_rows, roles) if administrator]
        unavailable = any(role is None for role in roles)
        if not result and unavailable:
            raise ManagementError(503, "暂时无法核实 Telegram 群管理员身份，请稍后重试")
        return result

    async def check_permissions(self, group_id: int, join_requests=False) -> dict:
        member = await self.tg.call("get_chat_member", chat_id=group_id, user_id=self.tg.user_id)
        missing = [
            name
            for name in ("can_delete_messages", "can_restrict_members")
            if not member.get(name) and member.get("status") != "creator"
        ]
        if not is_admin(member):
            missing.append("administrator")
        if join_requests and not member.get("can_invite_users") and member.get("status") != "creator":
            missing.append("can_invite_users")
        return {"ok": not missing, "missing": missing, "checked_at": now()}

    async def require_join_right(self, group_id):
        try:
            permissions = await self.check_permissions(group_id, True)
        except Exception as exc:
            raise ManagementError(503, "暂时无法检查申请入群权限") from exc
        if not permissions["ok"]:
            raise ManagementError(422, "申请入群验证缺少机器人权限：" + "、".join(permissions["missing"]))
