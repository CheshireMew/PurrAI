"""Telegram button routing and application command adaptation."""

from purrai.admin_labels import BOOLEANS, OPERATIONS
from purrai.bot_common import button
from purrai.errors import ManagementError
from purrai.schemas import (
    ArchiveFilter,
    EmojiPackChange,
    RuleInput,
    SettingsPatch,
    WhitelistInput,
)
from purrai.serialization import row_dict


class BotRoutes:
    def __init__(self, management, ui, state, panels, inputs):
        self.management, self.panels, self.inputs = management, panels, inputs
        self.send, self.panel, self.remember = ui.send, ui.panel, state.remember

    async def route(self, actor, token, record, message_id):
        action, group_id, data = record["action"], record["group_id"], record["data"]
        request_id = f"tg-button:{token}"
        if group_id is not None:
            await self.management.authorize(actor, group_id)
        if action not in ("prompt", "admin_add"):
            await self.remember(f"bot_prompt:{actor}", None)
        if action == "access_panel":
            await self.panels.access_panel(actor, data["id"], message_id)
        elif action == "group_access":
            await self.management.group_access(actor, data["id"], data["approved"])
            await self.panels.choose_groups(actor, message_id=message_id)
        elif action == "groups":
            await self.remember(f"bot_prompt:{actor}", None)
            await self.panels.choose_groups(actor, data.get("page", 0), message_id)
        elif action == "group":
            await self.panels.group_panel(actor, data["id"], message_id)
        elif action in ("activate", "permissions"):
            result = await self.management.permissions(actor, group_id, activate=action == "activate")
            await self.send(
                actor, "机器人权限完整。" if result["ok"] else "缺少权限：" + "、".join(result["missing"])
            )
            await self.panels.group_panel(actor, group_id, message_id)
        elif action == "pause":
            await self.management.settings_patch(actor, group_id, SettingsPatch(enabled=False), request_id)
            await self.panels.group_panel(actor, group_id, message_id)
        elif action == "join_link":
            result = await self.management.join_link(actor, group_id, request_id)
            await self.management.track(actor, group_id, actor, [result["id"]], request_id)
            await self.send(actor, "已受理，生成后会在此返回申请入群链接。请替换原有直接入群入口。")
        elif action == "settings":
            await self.panels.settings_panel(actor, group_id, data["section"], message_id)
        elif action == "chat_toggle":
            if data["key"] not in ("chat_enabled", "chat_proactive", "chat_stickers", "chat_antifraud"):
                raise ManagementError(422, "聊天配置项不存在")
            await self.management.setting(actor, group_id, data["key"], data["value"], request_id)
            await self.panels.chat_panel(actor, group_id, message_id)
        elif action == "chat_reset":
            await self.management.reset_chat(actor, group_id, request_id)
            await self.send(actor, "已重新开始聊天；历史归档仍然保留。")
            await self.panels.chat_panel(actor, group_id, message_id)
        elif action == "failure_policy":
            await self.management.setting(
                actor, group_id, "verification_failure_action", data["value"], request_id
            )
            await self.panels.settings_panel(actor, group_id, "verification", message_id)
        elif action == "archive":
            await self.panels.archive_panel(
                actor, group_id, data.get("filters"), data.get("page", 0), message_id
            )
        elif action == "archive_detail":
            await self.panels.archive_detail(actor, group_id, data, message_id)
        elif action == "archive_toggle":
            group = await self.management.authorize(actor, group_id)
            await self.management.setting(
                actor,
                group_id,
                "archive_enabled",
                not group.settings.get("archive_enabled", True),
                request_id,
            )
            await self.panels.archive_panel(actor, group_id, message_id=message_id)
        elif action == "archive_export":
            task = await self.management.archive_export(
                actor, group_id, ArchiveFilter.model_validate(data["filters"]), request_id
            )
            await self.management.track(actor, group_id, actor, [task["id"]], request_id)
            await self.send(actor, "导出已受理，完成后会在私聊发送 JSONL 文件；可在操作记录查看结果。")
        elif action == "toggle":
            if data["key"] not in BOOLEANS:
                raise ManagementError(422, "配置项不存在")
            await self.management.setting(actor, group_id, data["key"], data["value"], request_id)
            await self.panels.settings_panel(actor, group_id, data["section"], message_id)
        elif action == "list":
            await self.panels.list_panel(actor, group_id, data["kind"], data.get("page", 0), message_id)
        elif action == "detail":
            await self.panels.detail(actor, group_id, data["kind"], data["id"], message_id)
        elif action == "members":
            await self.panel(
                actor,
                "选择成员操作。下一步输入用户 ID 并核对确认。",
                [
                    button(OPERATIONS[k], "prompt", kind="member", operation=k)
                    for k in ("release", "reissue", "mute", "ban", "unmute", "unban")
                ]
                + [button("返回群管理", "group", id=group_id)],
                group_id,
                message_id,
            )
        elif action == "emoji_packs":
            await self.panels.emoji_packs_panel(actor, group_id, data.get("page", 0), message_id)
        elif action == "emoji_pack_remove":
            await self.management.change_status_packs(
                actor,
                group_id,
                EmojiPackChange(operation="remove", packs=[data["pack"]], request_id=request_id),
            )
            await self.send(actor, f"已从黑名单移除 {data['pack']}；已有禁言不会自动解除。")
            await self.panels.emoji_packs_panel(actor, group_id, data.get("page", 0), message_id)
        elif action == "emoji_packs_clear_confirm":
            await self.management.authorize(actor, group_id, write=True)
            await self.panel(
                actor,
                "确认清空本群的状态表情包黑名单？已有禁言不会自动解除。",
                [button("确认清空", "emoji_packs_clear"), button("取消并返回", "emoji_packs")],
                group_id,
                message_id,
            )
        elif action == "emoji_packs_clear":
            await self.management.change_status_packs(
                actor,
                group_id,
                EmojiPackChange(operation="clear", request_id=request_id),
            )
            await self.panels.emoji_packs_panel(actor, group_id, message_id=message_id)
        elif action == "prompt":
            await self.inputs.prompt(actor, group_id, data)
        elif action == "confirm":
            await self.inputs.confirmation(actor, group_id, data, message_id)
        elif action == "execute":
            await self.inputs.submit(actor, group_id, actor, data, request_id)
        elif action in ("rule_toggle", "rule_mode", "rule_scope", "rule_penalty"):
            row = await self.management.queries.single(actor, group_id, "rule", data["id"])
            if not row or row.group_id != group_id:
                raise ManagementError(404, "规则不存在")
            key = {
                "rule_toggle": "enabled",
                "rule_mode": "mode",
                "rule_scope": "scope",
                "rule_penalty": "penalty",
            }[action]
            values = {**row_dict(row), key: data[key]}
            await self.management.rule(actor, group_id, RuleInput(**values), row.id, request_id)
            await self.panels.detail(actor, group_id, "rule", row.id, message_id)
        elif action == "white_toggle":
            row = await self.management.queries.single(actor, group_id, "whitelist", data["id"])
            if not row or data["key"] not in ("verification", "moderation"):
                raise ManagementError(404, "白名单记录不存在")
            await self.management.whitelist(
                actor, group_id, WhitelistInput(**{**row_dict(row), data["key"]: data["value"]}), request_id
            )
            await self.panels.detail(actor, group_id, "whitelist", row.user_id, message_id)
        elif action == "admins":
            await self.panels.admins(actor, message_id, data.get("page", 0))
        elif action in ("admin_add", "admin_detail", "admin_grant", "admin_active"):
            await self.send(actor, "已改为自动识别群管理员，请在 Telegram 群设置中管理身份。")
            await self.panels.admins(actor, message_id)
        elif action == "system":
            await self.panels.system(actor, message_id)
        elif action == "failures":
            await self.panels.failures(actor, data.get("event", False), data.get("page", 0), message_id)
        elif action == "retry":
            event = data.get("event", False)
            await self.management.retry(actor, data["id"], event, request_id)
            if not event:
                row = await self.management.queries.action(actor, data["id"])
                if row["group_id"]:
                    await self.management.track(actor, row["group_id"], actor, [row["id"]], request_id)
            await self.send(actor, "重试已受理。可返回系统状态查看实际处理结果。")
            await self.panels.failures(actor, event, message_id=message_id)
        elif action == "backup":
            result = await self.management.backup(actor, request_id)
            await self.send(actor, f"一致性备份已保存：{result['file']}")
        else:
            if group_id is not None:
                await self.panels.group_panel(actor, group_id, message_id)
            else:
                await self.panels.choose_groups(actor, message_id=message_id)
