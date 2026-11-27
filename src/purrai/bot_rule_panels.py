"""Telegram rule and whitelist editing, separate from record presentation."""

from purrai.bot_common import user_note
from purrai.errors import ManagementError
from purrai.schemas import PreviewInput, RuleInput, WhitelistInput


class RulePanels:
    ACTIONS = ("rule_inherit", "rule_toggle", "rule_mode", "rule_scope", "rule_penalty", "white_toggle")
    INPUTS = ("rule", "preview", "rule_context", "whitelist", "white_note")

    def __init__(self, management, ui, records):
        self.management, self.records = management, records
        self.send = ui.send

    async def route(self, actor, group_id, action, data, request_id, message_id):
        if action == "rule_inherit":
            await self.management.rules.inherit(actor, group_id, data["id"], data.get("version"), request_id)
            await self.records.detail(actor, group_id, "rule", data["id"], message_id)
        elif action in ("rule_toggle", "rule_mode", "rule_scope", "rule_penalty"):
            key = {
                "rule_toggle": "enabled",
                "rule_mode": "mode",
                "rule_scope": "scope",
                "rule_penalty": "penalty",
            }[action]
            await self.management.rules.patch_rule(
                actor, group_id, data["id"], {key: data[key]}, request_id, data.get("version")
            )
            await self.records.detail(actor, group_id, "rule", data["id"], message_id)
        elif action == "white_toggle":
            if data["key"] not in ("verification", "moderation"):
                raise ManagementError(404, "白名单记录不存在")
            await self.management.rules.patch_whitelist(
                actor,
                group_id,
                int(data["id"]),
                {data["key"]: data["value"]},
                request_id,
                data.get("version"),
            )
            await self.records.detail(actor, group_id, "whitelist", data["id"], message_id)

    async def input(self, actor, group_id, text, data, request_id):
        kind = data["kind"]
        if kind == "rule":
            if data.get("id"):
                await self.management.rules.patch_rule(
                    actor, group_id, data["id"], {"value": text}, request_id, data.get("version")
                )
            else:
                await self.management.rule(
                    actor, group_id, RuleInput(kind=data["rule_kind"], value=text), request_id=request_id
                )
            await self.send(actor, "规则已保存。")
            await self.records.list_panel(actor, group_id, "rule")
        elif kind == "preview":
            name, body = ("", text)
            if text.startswith("昵称:") or text.startswith("昵称："):
                first, _, body = text.partition("\n")
                name = first[3:]
            result = await self.management.preview(actor, group_id, PreviewInput(text=body, sender_name=name))
            await self.send(
                actor,
                "规则测试结果：\n"
                + ("；".join(result["reasons"]) or "未命中当前规则")
                + f"\n将禁言：{('是' if result['mute_member'] else '否')}；将删消息：{('是' if result['delete_message'] else '否')}"
                + f"；将踢出：{('是' if result['kick_member'] else '否')}"
                + f"；将禁止重新加入：{('需核实链接' if result.get('notes') and (not result['ban_member']) else '是' if result['ban_member'] else '否')}"
                + ("\n" + "\n".join(result["notes"]) if result.get("notes") else ""),
            )
        elif kind == "rule_context":
            await self.management.rules.patch_rule(
                actor,
                group_id,
                data["id"],
                {"scope": "name_and_message", "context_value": text},
                request_id,
                data.get("version"),
            )
            await self.records.detail(actor, group_id, "rule", data["id"])
        elif kind == "whitelist":
            target, note = user_note(text)
            await self.management.whitelist(
                actor,
                group_id,
                WhitelistInput(user_id=target, note=note),
                request_id,
                expected_version="missing",
            )
            await self.send(actor, "已加入本群白名单。")
            await self.records.list_panel(actor, group_id, "whitelist")
        elif kind == "white_note":
            await self.management.rules.patch_whitelist(
                actor, group_id, int(data["id"]), {"note": text}, request_id, data.get("version")
            )
            await self.records.detail(actor, group_id, "whitelist", data["id"])
