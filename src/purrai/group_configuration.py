"""Administration operations for group configuration."""

from purrai.errors import ManagementError
from purrai.models import (
    Group,
)
from purrai.pack_names import DEFAULT_BLOCKED_PACKS
from purrai.schemas import (
    EmojiPackChange,
    GroupSettings,
    SettingsPatch,
)
from purrai.serialization import row_dict


class GroupConfiguration:
    def __init__(self, context):
        self.context = context

    async def settings_patch(self, actor, group_id, body: SettingsPatch, request_id=""):
        return await self.update(actor, group_id, body, request_id, "修改群配置")

    async def setting(self, actor, group_id, key, value, request_id):
        if key not in GroupSettings.model_fields:
            raise ManagementError(422, "配置项不存在")
        return await self.update(
            actor,
            group_id,
            SettingsPatch(settings={key: value}),
            request_id,
            f"Telegram 修改配置：{key}",
            validate_unchanged_packs=key == "emoji_status_blocked_packs",
        )

    async def update(self, actor, group_id, body, request_id, reason, *, validate_unchanged_packs=False):
        current = await self.context.access.authorize(actor, group_id, write=True)
        changes = body.settings or {}
        if "emoji_status_blocked_packs" in changes and (
            validate_unchanged_packs
            or changes["emoji_status_blocked_packs"]
            != current.settings.get("emoji_status_blocked_packs", list(DEFAULT_BLOCKED_PACKS))
        ):
            await self.validate_status_packs(changes["emoji_status_blocked_packs"])
        if changes.get("join_requests"):
            await self.context.access.require_join_right(group_id)
        if body.enabled is True:
            raise ManagementError(422, "启用保护请使用群接入检查操作")
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id)
            if previous is not None:
                return previous
            group = await session.get(Group, group_id)
            if changes and body.baseline is not None:
                current_values = GroupSettings.model_validate(group.settings).model_dump()
                conflicts = [
                    key
                    for key, value in changes.items()
                    if key not in body.baseline or current_values[key] not in (body.baseline[key], value)
                ]
                if conflicts:
                    raise ManagementError(
                        409, "这些设置已被其他管理员修改，请重新打开群设置后再保存：" + "、".join(conflicts)
                    )
            if body.enabled is False:
                group.enabled, group.status = False, "paused"
            if changes:
                group.settings = GroupSettings.model_validate({**group.settings, **changes}).model_dump()
            return await self.context.record(
                session, actor, group_id, "settings", reason, row_dict(group), request_id
            )

    async def change_status_packs(self, actor, group_id, body: EmojiPackChange):
        group = await self.context.access.authorize(actor, group_id, write=True)
        async with self.context.db.sessions() as session:
            previous = await self.context.previous(session, actor, body.request_id)
        if previous is not None:
            return self.status_pack_replay(previous, group_id, body)
        if body.operation == "add":
            current = group.settings.get("emoji_status_blocked_packs", list(DEFAULT_BLOCKED_PACKS))
            await self.validate_status_packs([pack for pack in body.packs if pack not in current])
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, body.request_id)
            if previous is not None:
                return self.status_pack_replay(previous, group_id, body)
            group = await session.get(Group, group_id)
            if not group or not group.approved:
                raise ManagementError(403, "此群尚未获准接入")
            current = group.settings.get("emoji_status_blocked_packs", list(DEFAULT_BLOCKED_PACKS))
            if body.operation == "add":
                updated = list(dict.fromkeys([*current, *body.packs]))
            elif body.operation == "remove":
                updated = [pack for pack in current if pack not in body.packs]
            else:
                updated = []
            if len(updated) > 100:
                raise ManagementError(422, "每群最多添加 100 个状态表情包，请先移除不再需要的条目")
            group.settings = {**group.settings, "emoji_status_blocked_packs": updated}
            result = {
                "group_id": group_id,
                "operation": body.operation,
                "requested_packs": body.packs,
                "packs": updated,
                "added": [pack for pack in updated if pack not in current],
                "removed": [pack for pack in current if pack not in updated],
            }
            return await self.context.record(
                session,
                actor,
                group_id,
                "settings",
                {"add": "新增", "remove": "移除", "clear": "清空"}[body.operation] + "状态表情包黑名单",
                result,
                body.request_id,
            )

    @staticmethod
    def status_pack_replay(previous, group_id, body):
        if (
            previous.get("group_id") != group_id
            or previous.get("operation") != body.operation
            or previous.get("requested_packs") != body.packs
        ):
            raise ManagementError(409, "请求标识已用于其他操作")
        return previous

    async def validate_status_packs(self, packs):
        from aiogram.exceptions import TelegramBadRequest

        for name in packs:
            try:
                result = await self.context.tg.call("get_sticker_set", name=name, request_timeout=3)
            except TelegramBadRequest as exc:
                raise ManagementError(422, f"表情包不存在或无法读取：{name}") from exc
            except Exception as exc:
                raise ManagementError(503, "暂时无法向 Telegram 核实表情包，请稍后重试") from exc
            if result.get("sticker_type") != "custom_emoji" or result.get("name", "").lower() != name:
                raise ManagementError(422, f"请使用自定义表情包的 addemoji 链接：{name}")
