"""Telegram input parsing, confirmations and command submission."""

from datetime import datetime
from zoneinfo import ZoneInfo

from purrai.admin_labels import CHAT_FIELDS, NUMBERS, OPERATIONS
from purrai.bot_common import button
from purrai.errors import ManagementError
from purrai.models import now
from purrai.pack_names import DEFAULT_BLOCKED_PACKS
from purrai.schemas import (
    ActionInput,
    ArchiveFilter,
    EmojiPackChange,
    GroupSettings,
    PreviewInput,
    RuleInput,
    WhitelistInput,
)
from purrai.serialization import row_dict


class BotInput:
    def __init__(self, management, ui, state, panels):
        self.management, self.panels = management, panels
        self.send, self.panel, self.remember = ui.send, ui.panel, state.remember

    async def prompt(self, actor, group_id, data):
        await self.management.authorize(actor, group_id, write=True)
        group = await self.management.authorize(actor, group_id)
        kind = data["kind"]
        instructions = {
            "emoji_packs": "请发送要新增的 t.me/addemoji 链接或表情包短名称，多个用空格、逗号或换行分隔。只追加新条目，原名单自动保留并去重。移除或清空请返回黑名单面板操作。仅检查名字旁的表情状态，命中永久禁言。"
            + f"当前已有 {len(group.settings.get('emoji_status_blocked_packs', DEFAULT_BLOCKED_PACKS))} 个，可返回面板查看。",
            "archive_query": "请发送要搜索的文字（包含历史编辑版本），发送 - 清除关键词。",
            "archive_sender": "请发送用户或频道的数字 ID，发送 - 清除发送者筛选。",
            "archive_dates": "请发送北京时间范围，例如 2026-10-01 00:00 / 2026-10-20 00:00；发送 - 清除时间筛选。",
            "setting": f"请发送新的 {NUMBERS.get(data.get('key'), '数值')}。",
            "chat_setting": f"请发送新的{CHAT_FIELDS.get(data.get('key'), '聊天设置')}。性格直接用自然语言描述即可。",
            "rule": "请发送一条规则内容。域名只填主机名，例如 example.com；关键词默认包含匹配。",
            "preview": "请发送要测试的消息，不会执行处罚。",
            "whitelist": "请发送 Telegram 用户 ID，可在空格后填写备注。默认免验证、免自动处罚。",
            "white_note": "请发送新的备注。",
            "rule_context": "现有规则内容作为昵称关键词。请发送正文中还需同时命中的关键词。昵称和正文同时命中时永久禁言并删消息。",
            "member": f"{OPERATIONS.get(data.get('operation'), '成员操作')}：请发送 Telegram 用户 ID，可在空格后填写原因。",
        }
        await self.remember(
            f"bot_prompt:{actor}", {"group_id": group_id, "data": data, "expires": now() + 900}
        )
        field = GroupSettings.model_json_schema()["properties"].get(data.get("key"), {})
        if kind in ("setting", "chat_setting") and "minimum" in field and "maximum" in field:
            instructions[kind] += f"允许范围：{field['minimum']} 至 {field['maximum']}。"
        await self.send(actor, f"{group.title}\n{instructions[kind]}\n发送 /cancel 取消；15 分钟内有效。")

    async def confirmation(self, actor, group_id, data, message_id=None):
        group = await self.management.authorize(actor, group_id, write=True)
        descriptions = {"batch": "批量放行所有正在验证的成员", "white_remove": "移出白名单"}
        operation = descriptions.get(data["kind"], OPERATIONS.get(data.get("operation"), "执行管理操作"))
        duration = ""
        if data.get("operation") == "mute":
            seconds = group.settings["mute_seconds"]
            duration = (
                f"\n本群禁言时长：{seconds // 86400} 天"
                if seconds % 86400 == 0
                else f"\n本群禁言时长：{seconds} 秒"
            )
        elif data.get("operation") == "ban":
            duration = f"\n本群临时封禁时长：{group.settings['failure_ban_seconds']} 秒"
        text = f"请确认 · {group.title}\n操作：{operation}\n用户：{data.get('user_id', data.get('id', '全部待验证成员'))}{duration}\n原因：{data.get('reason', 'Telegram 管理员操作')}"
        await self.panel(
            actor,
            text,
            [button("确认执行", "execute", **data), button("取消并返回", "group", id=group_id)],
            group_id,
            message_id,
        )

    async def submit(self, actor, group_id, chat_id, data, request_id):
        group = await self.management.authorize(actor, group_id, write=True)
        body = ActionInput(
            action=data.get("operation", "release"),
            reason=data.get("reason") or "Telegram 管理员操作",
            request_id=request_id,
        )
        if data["kind"] == "batch":
            result = await self.management.release_pending(actor, group_id, body)
        elif data["kind"] == "verification":
            result = await self.management.verification_action(actor, data["id"], body)
        elif data["kind"] == "violation":
            result = await self.management.violation_action(actor, group_id, data["id"], body)
        elif data["kind"] == "white_remove":
            await self.management.whitelist(
                actor, group_id, WhitelistInput(user_id=int(data["id"])), request_id, remove=True
            )
            await self.send(chat_id, "已移出本群白名单。")
            return
        else:
            result = await self.management.member_action(
                actor, group_id, int(data["user_id"]), body.action, request_id, body.reason
            )
        identifiers = result.get("action_ids", [result["id"]] if "id" in result else [])
        await self.management.track(actor, group_id, chat_id, identifiers, request_id)
        await self.send(
            chat_id,
            f"{group.title}\n已提交 {len(identifiers)} 项操作。Telegram 执行后会在此反馈结果；也可在操作记录查看。",
        )

    async def input(self, actor, text, prompt, request_id):
        group_id, data = prompt["group_id"], prompt["data"]
        kind = data["kind"]
        if kind == "chat_setting":
            key = data["key"]
            if key not in CHAT_FIELDS:
                raise ManagementError(422, "聊天配置项不存在")
            value = text
            if key in ("chat_history", "chat_interval", "chat_sticker_interval", "chat_antifraud_interval"):
                if not text.isdecimal():
                    raise ManagementError(422, "请发送有效的整数")
                value = int(text)
            elif key in ("chat_sticker_packs", "chat_antifraud_packs"):
                import re

                value = [] if text.strip() == "-" else re.split(r"[\s,，]+", text.strip())
            elif key == "chat_ignored_texts":
                value = [] if text.strip() == "-" else text.splitlines()
            await self.management.setting(actor, group_id, key, value, request_id)
            await self.panels.chat_panel(actor, group_id)
        elif kind.startswith("archive_"):
            filters = dict(data.get("filters", {}))
            if kind == "archive_query":
                filters["query"] = "" if text == "-" else text
            elif kind == "archive_sender":
                try:
                    filters["sender_id"] = None if text == "-" else int(text)
                except ValueError as exc:
                    raise ManagementError(422, "请发送有效的数字 ID，或发送 - 清除") from exc
            elif kind == "archive_dates":
                if text == "-":
                    filters.update(after=0, before=0)
                else:
                    try:
                        start, end = text.split("/", 1)
                        filters.update(
                            after=datetime.strptime(start.strip(), "%Y-%m-%d %H:%M")
                            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                            .timestamp(),
                            before=datetime.strptime(end.strip(), "%Y-%m-%d %H:%M")
                            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                            .timestamp(),
                        )
                    except ValueError as exc:
                        raise ManagementError(
                            422, "时间格式示例：2026-10-01 00:00 / 2026-10-20 00:00"
                        ) from exc
            ArchiveFilter.model_validate(filters)
            await self.remember(f"bot_prompt:{actor}", None)
            await self.panels.archive_panel(actor, group_id, filters)
        elif kind == "emoji_packs":
            import re

            if text.strip() == "-":
                raise ManagementError(
                    422, "这里只新增，不会清空名单。请发送 /cancel 后在黑名单面板点击清空。"
                )
            body = EmojiPackChange(
                packs=[item for item in re.split(r"[\s,，]+", text.strip()) if item], request_id=request_id
            )
            result = await self.management.change_status_packs(actor, group_id, body)
            await self.send(
                actor,
                f"已新增 {len(result['added'])} 个表情包，原名单已保留。"
                if result["added"]
                else "这些表情包已在黑名单中，无需重复添加。",
            )
            await self.panels.emoji_packs_panel(actor, group_id)
        elif kind == "setting":
            if not text.isdecimal():
                raise ManagementError(422, "请发送有效的非负整数；发送 /cancel 可取消")
            await self.management.setting(actor, group_id, data["key"], int(text), request_id)
            await self.send(actor, "群设置已保存。")
            await self.panels.settings_panel(actor, group_id, data["section"])
        elif kind == "rule":
            values = {"kind": data["rule_kind"], "value": text}
            if data.get("id"):
                old = await self.management.queries.single(actor, group_id, "rule", data["id"])
                if not old or old.group_id != group_id:
                    raise ManagementError(404, "规则不存在")
                values = {**row_dict(old), "value": text}
            await self.management.rule(actor, group_id, RuleInput(**values), data.get("id", ""), request_id)
            await self.send(actor, "规则已保存。")
            await self.panels.list_panel(actor, group_id, "rule")
        elif kind == "preview":
            name, body = "", text
            if text.startswith("昵称:") or text.startswith("昵称："):
                first, _, body = text.partition("\n")
                name = first[3:]
            result = await self.management.preview(actor, group_id, PreviewInput(text=body, sender_name=name))
            await self.send(
                actor,
                "规则测试结果：\n"
                + ("；".join(result["reasons"]) or "未命中当前规则")
                + f"\n将禁言：{'是' if result['mute_member'] else '否'}；将删消息：{'是' if result['delete_message'] else '否'}"
                + f"；将踢出：{'是' if result['kick_member'] else '否'}",
            )
        elif kind == "rule_context":
            old = await self.management.queries.single(actor, group_id, "rule", data["id"])
            if not old or old.group_id != group_id:
                raise ManagementError(404, "规则不存在")
            await self.management.rule(
                actor,
                group_id,
                RuleInput(**{**row_dict(old), "scope": "name_and_message", "context_value": text}),
                old.id,
                request_id,
            )
            await self.panels.detail(actor, group_id, "rule", old.id)
        elif kind in ("member", "whitelist"):
            parts = text.split(maxsplit=1)
            if not parts or not parts[0].isdecimal() or int(parts[0]) <= 0:
                raise ManagementError(422, "请发送数字形式的 Telegram 用户 ID")
            target, note = int(parts[0]), parts[1] if len(parts) > 1 else ""
            if kind == "whitelist":
                await self.management.whitelist(
                    actor, group_id, WhitelistInput(user_id=target, note=note), request_id
                )
                await self.send(actor, "已加入本群白名单。")
                await self.panels.list_panel(actor, group_id, "whitelist")
            else:
                await self.confirmation(
                    actor,
                    group_id,
                    {"kind": "member", "operation": data["operation"], "user_id": target, "reason": note},
                )
        elif kind == "white_note":
            old = await self.management.queries.single(actor, group_id, "whitelist", data["id"])
            if not old:
                raise ManagementError(404, "白名单记录不存在")
            await self.management.whitelist(
                actor, group_id, WhitelistInput(**{**row_dict(old), "note": text}), request_id
            )
            await self.panels.detail(actor, group_id, "whitelist", data["id"])
        await self.remember(f"bot_prompt:{actor}", None)
