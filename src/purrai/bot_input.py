"""Telegram input parsing, confirmations and command submission."""


from purrai.admin_labels import CHAT_FIELDS, NUMBERS, OPERATIONS
from purrai.bot_common import button, duration_reason, user_note
from purrai.errors import ManagementError
from purrai.models import now
from purrai.pack_names import DEFAULT_BLOCKED_PACKS
from purrai.schemas import (
    ActionInput,
    GroupSettings,
    ReleasePendingInput,
    ReleaseSelectionInput,
    WhitelistInput,
)


class BotInput:
    def __init__(self, management, ui, state, panels):
        self.management, self.panels = management, panels
        self.send, self.panel, self.remember = ui.send, ui.panel, state.remember
        self.handlers = dict(panels.input_handlers)
        panels.register(self.handlers, ("member",), (self.member_input, True))
        self.prefix_handlers = panels.input_prefixes

    async def admission_prompt(self, actor):
        await self.management.principal(actor, root=True)
        await self.remember(f"bot_prompt:{actor}", {
            "group_id": None, "data": {"kind": "group_admission"}, "expires": now() + 900,
        })
        await self.send(actor,
            "请发送要接入的群数字 ID，例如 -1001234567890。公开群和私密群均可，机器人须已加入群。"
            "接入后可在群设置中开启保护和聊天。邀请链接不能代替群 ID。\n发送 /cancel 取消；15 分钟内有效。")

    async def prompt(self, actor, group_id, data):
        await self.management.authorize(actor, group_id, write=True)
        group = await self.management.authorize(actor, group_id)
        kind = data["kind"]
        instructions = {
            "emoji_packs": "请发送要新增的 t.me/addemoji 链接或表情包短名称，多个用空格、逗号或换行分隔。只追加新条目，原名单自动保留并去重。移除或清空请返回黑名单面板操作。仅检查名字旁的表情状态；入群命中永久封禁，群内发言命中永久禁言。"
            + f"当前已有 {len(group.settings.get('emoji_status_blocked_packs', DEFAULT_BLOCKED_PACKS))} 个，可返回面板查看。",
            "archive_query": "请发送要搜索的文字（包含历史编辑版本），发送 - 清除关键词。",
            "archive_sender": "请发送用户或频道的数字 ID，发送 - 清除发送者筛选。",
            "archive_dates": "请发送北京时间范围，例如 2026-10-01 00:00 / 2026-10-20 00:00；发送 - 清除时间筛选。",
            "setting": f"请发送新的 {NUMBERS.get(data.get('key'), '数值')}。",
            "verification_channel": "请发送公开频道的 @用户名或 t.me 链接。机器人须为频道管理员。用户订阅后自动放行，无需点击检查。发送 - 清空（请先改用图片或文字验证）。",
            "telegram_links": "请发送完整的允许链接名单，每行一个 Telegram 群组、频道或邀请链接；发送 - 清空。名单只豁免链接本身，明确广告招揽仍会处罚。当前名单：\n" + "\n".join(group.settings.get("telegram_allowed_links", [])),
            "chat_setting": f"请发送新的{CHAT_FIELDS.get(data.get('key'), '聊天设置')}。性格直接用自然语言描述即可。",
            "rule": "请发送一条规则内容。域名只填主机名，例如 example.com；关键词默认包含匹配。",
            "preview": "请发送要测试的消息，不会执行处罚。",
            "whitelist": "请发送 Telegram 用户 ID，可在空格后填写备注。默认免验证、免自动处罚。",
            "member_lookup": "请发送要查询成员的 Telegram 数字 ID。",
            "white_note": "请发送新的备注。",
            "rule_context": "现有规则内容作为昵称关键词。请发送正文中还需同时命中的关键词。昵称和正文同时命中时永久禁言并删消息。",
            "member": f"{OPERATIONS.get(data.get('operation'), '成员操作')}：请发送 Telegram 用户 ID，可在空格后填写原因。禁言或封禁可指定本次时长，例如 123456 30m 原因（s 秒、m 分、h 小时、d 天），省略时长沿用本群设置。",
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
        if data.get("duration_seconds") is not None and not 30 <= data["duration_seconds"] <= 2592000:
            raise ManagementError(422, "本次禁言或封禁时长须在 30 秒到 30 天之间")
        descriptions = {"batch": "批量放行所有正在验证的成员", "white_remove": "移出白名单"}
        operation = descriptions.get(data["kind"], OPERATIONS.get(data.get("operation"), "执行管理操作"))
        batch_preview = None
        if data["kind"] == "batch":
            batch_preview = await self.management.members.release_preview(actor, group_id, ReleaseSelectionInput())
            if not batch_preview["total"]:
                raise ManagementError(409, "当前没有待放行成员")
            data = {**data, "verification_ids": [item["id"] for item in batch_preview["items"]],
                    "snapshot": batch_preview["snapshot"]}
        duration = ""
        if data.get("operation") == "mute":
            seconds = data.get("duration_seconds") or group.settings["mute_seconds"]
            duration = (
                f"\n本次禁言时长：{seconds // 86400} 天"
                if seconds % 86400 == 0
                else f"\n本次禁言时长：{seconds} 秒"
            )
        elif data.get("operation") == "ban":
            duration = f"\n本次临时封禁时长：{data.get('duration_seconds') or group.settings['failure_ban_seconds']} 秒"
        text = f"请确认 · {group.title}\n操作：{operation}\n用户：{data.get('user_id', data.get('id', '全部待验证成员'))}{duration}\n原因：{data.get('reason', 'Telegram 管理员操作')}"
        if batch_preview:
            text += f"\n本次固定 {batch_preview['total']} 位成员，新入群者不会纳入。\n" + "\n".join(
                f"{item['user_name']} · {item['user_id']}" for item in batch_preview["items"][:30])
            if batch_preview["total"] > 30:
                text += "\n完整名单可在待验证列表核对；超过 30 人建议使用网页逐页核对后提交。"
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
            duration_seconds=data.get("duration_seconds"),
        )
        if data["kind"] == "batch":
            result = await self.management.release_pending(actor, group_id, ReleasePendingInput(**body.model_dump(),
                verification_ids=data.get("verification_ids"), snapshot=data.get("snapshot", "")))
        elif data["kind"] == "verification":
            result = await self.management.verification_action(actor, data["id"], body)
        elif data["kind"] == "violation":
            result = await self.management.violation_action(actor, group_id, data["id"], body)
        elif data["kind"] == "white_remove":
            await self.management.whitelist(
                actor, group_id, WhitelistInput(user_id=int(data["id"])), request_id, remove=True,
                expected_version=data.get("version")
            )
            await self.send(chat_id, "已移出本群白名单。")
            return
        else:
            result = await self.management.member_action(
                actor, group_id, int(data["user_id"]), body.action, request_id, body.reason, body.duration_seconds
            )
        identifiers = result.get("action_ids", [result["id"]] if "id" in result else [])
        await self.management.track(actor, group_id, chat_id, identifiers, request_id)
        if chat_id != group_id or group.settings.get("admin_submission_notice", False):
            await self.send(
                chat_id,
                f"{group.title}\n已提交 {len(identifiers)} 项操作。Telegram 执行后会在此反馈结果；也可在操作记录查看。",
                request_id=request_id,
            )

    async def input(self, actor, text, prompt, request_id):
        group_id, data = prompt["group_id"], prompt["data"]
        kind = data["kind"]
        handler, clear_prompt = self.handlers.get(kind, (None, True))
        if handler is None:
            for prefix, candidate, clear in self.prefix_handlers:
                if kind.startswith(prefix):
                    handler, clear_prompt = candidate, clear
                    break
        if handler:
            await handler(actor=actor, group_id=group_id, text=text, data=data, request_id=request_id)
        if clear_prompt:
            await self.remember(f"bot_prompt:{actor}", None)

    async def member_input(self, actor, group_id, text, data, request_id):
        target, note = user_note(text)
        seconds, note = duration_reason(note, data['operation'])
        await self.confirmation(actor, group_id, {'kind': 'member', 'operation': data['operation'], 'user_id': target, 'reason': note, 'duration_seconds': seconds})
