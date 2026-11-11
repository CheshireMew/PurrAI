"""Telegram editing of common policy and individual group inheritance."""

import re

from purrai.admin_catalog import admin_catalog
from purrai.admin_labels import KINDS, MODES, SCOPES
from purrai.bot_common import button
from purrai.errors import ManagementError
from purrai.models import now
from purrai.rule_presentation import describe_rule
from purrai.schemas import GroupSettings, SettingsPatch, SharedPolicyPatch


def shown(value):
    if isinstance(value, bool):
        return "开启" if value else "关闭"
    if isinstance(value, list):
        return "、".join(value) or "空"
    return str(value)


class PolicyPanels:
    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.remember = state.remember

    async def route(self, actor, action, group_id, data, request_id, message_id):
        if action == "policy_inheritance":
            return await self.inheritance(actor, group_id, data.get("page", 0), message_id)
        if action == "policy_reset":
            key = data["key"]
            await self.management.settings_patch(actor, group_id,
                SettingsPatch(reset=[key], baseline={key: data["baseline"]}), request_id)
            return await self.inheritance(actor, group_id, data.get("page", 0), message_id)
        await self.management.principal(actor, root=True)
        if action == "policy_home":
            return await self.home(actor, message_id)
        if action == "policy_settings":
            return await self.settings(actor, data.get("page", 0), message_id)
        if action == "policy_field":
            return await self.field(actor, data["key"], message_id)
        if action == "policy_set":
            await self.management.configuration.shared(actor, SharedPolicyPatch(settings={data["key"]: data["value"]},
                baseline={"settings": {data["key"]: data["baseline"]}}, request_id=request_id))
            return await self.field(actor, data["key"], message_id)
        if action == "policy_rules":
            return await self.rules(actor, data.get("page", 0), message_id)
        if action == "policy_rule":
            return await self.rule(actor, data["id"], message_id)
        if action == "policy_new_rule":
            return await self.panel(actor, "选择通用规则类型。保存后应用到所有继承的群。",
                [button(name, "policy_prompt", kind="policy_rule_value", rule_kind=kind) for kind, name in KINDS.items()]
                + [button("返回通用规则", "policy_rules")], message_id=message_id,
                resume={"action": "policy_new_rule", "data": {}})
        if action == "policy_rule_change":
            baseline = data["baseline"]
            await self.save_rule(actor, {**baseline, **data["changes"]}, baseline, request_id)
            return await self.rule(actor, baseline["id"], message_id)
        if action == "policy_prompt":
            return await self.prompt(actor, data)
        raise ManagementError(422, "通用配置操作不存在")

    async def home(self, actor, message_id=None):
        await self.management.principal(actor, root=True)
        await self.panel(actor, "通用设置与规则\n修改应用到未单独覆盖相应项目的群。已有成员处罚不会自动撤销。",
            [button("通用群设置", "policy_settings"), button("通用广告规则与推广目标", "policy_rules"),
             button("返回选群", "groups")], message_id=message_id, resume={"action": "policy_home", "data": {}})

    async def settings(self, actor, page=0, message_id=None):
        current = await self.management.configuration.shared(actor)
        keys = [key for key in current["settings"] if key not in ("warning_window", "warning_limit", "newcomer_contacts")]
        labels = admin_catalog()["settings"]["labels"]
        page = max(0, min(page, (len(keys) - 1) // 6))
        buttons = [button(f"{labels[key]}：{shown(current['settings'][key])}"[:60], "policy_field", key=key)
                   for key in keys[page * 6:page * 6 + 6]]
        if page:
            buttons.append(button("上一页", "policy_settings", page=page - 1))
        if (page + 1) * 6 < len(keys):
            buttons.append(button("下一页", "policy_settings", page=page + 1))
        buttons.append(button("返回通用管理", "policy_home"))
        await self.panel(actor, f"通用群设置 · 第 {page + 1} 页", buttons, message_id=message_id,
                         resume={"action": "policy_settings", "data": {"page": page}})

    async def field(self, actor, key, message_id=None):
        current = await self.management.configuration.shared(actor)
        if key not in GroupSettings.model_fields:
            raise ManagementError(422, "设置不存在")
        value = current["settings"][key]
        field = GroupSettings.model_json_schema()["properties"][key]
        if isinstance(value, bool):
            buttons = [button("改为" + shown(not value), "policy_set", key=key, value=not value, baseline=value)]
        elif "enum" in field:
            names = {"kick": "踢出（允许重新加入）", "permanent_mute": "永久禁言（留群）"}
            buttons = [button(names.get(item, item), "policy_set", key=key, value=item, baseline=value)
                       for item in field["enum"]]
        else:
            buttons = [button("修改此项", "policy_prompt", kind="policy_setting", key=key, baseline=value)]
        buttons.append(button("返回通用群设置", "policy_settings"))
        await self.panel(actor, f"{admin_catalog()['settings']['labels'][key]}\n当前通用值：{shown(value)}\n"
                         "保存会更新仍跟随通用值的群。", buttons, message_id=message_id,
                         resume={"action": "policy_field", "data": {"key": key}})

    async def rules(self, actor, page=0, message_id=None):
        rules = (await self.management.configuration.shared(actor))["rules"]
        page = max(0, min(page, max(0, (len(rules) - 1) // 5)))
        buttons = [button(f"{'启用' if item['enabled'] else '停用'} · {KINDS[item['kind']]} · {item['value']}",
                          "policy_rule", id=item["id"]) for item in rules[page * 5:page * 5 + 5]]
        if page:
            buttons.append(button("上一页", "policy_rules", page=page - 1))
        if len(rules) > page * 5 + 5:
            buttons.append(button("下一页", "policy_rules", page=page + 1))
        buttons += [button("新增通用规则", "policy_new_rule"), button("返回通用管理", "policy_home")]
        await self.panel(actor, f"通用广告规则与推广目标 · 共 {len(rules)} 条 · 第 {page + 1} 页", buttons,
                         message_id=message_id, resume={"action": "policy_rules", "data": {"page": page}})

    async def rule(self, actor, identifier, message_id=None):
        rules = (await self.management.configuration.shared(actor))["rules"]
        original = next((item for item in rules if item["id"] == identifier), None)
        if original is None:
            raise ManagementError(404, "通用规则不存在")
        item = describe_rule(original)
        buttons = [button("停用通用规则" if item["enabled"] else "启用通用规则", "policy_rule_change",
                          baseline=original, changes={"enabled": not item["enabled"]}),
                   button("修改内容", "policy_prompt", kind="policy_rule_value", baseline=original)]
        if item["kind"] == "keyword":
            modes = list(MODES)
            scopes = ["message", "name", "either"]
            scope = scopes[(scopes.index(item["scope"]) + 1) % 3] if item["scope"] in scopes else "message"
            buttons += [button("匹配改为" + MODES[modes[(modes.index(item["mode"]) + 1) % len(modes)]],
                               "policy_rule_change", baseline=original,
                               changes={"mode": modes[(modes.index(item["mode"]) + 1) % len(modes)]}),
                        button("范围改为" + SCOPES[scope], "policy_rule_change", baseline=original, changes={"scope": scope}),
                        button("设置昵称＋正文组合", "policy_prompt", kind="policy_rule_context", baseline=original),
                        button("正文改为只删消息" if item["penalty"] == "permanent_mute" else "正文改为永久禁言",
                               "policy_rule_change", baseline=original,
                               changes={"penalty": "delete" if item["penalty"] == "permanent_mute" else "permanent_mute"})]
        buttons.append(button("返回通用规则", "policy_rules"))
        await self.panel(actor, f"{item['kind_label']}：{item['value']}\n{item['matching_label']}\n{item['consequence']}\n"
                         "此处修改通用规则；各群已有的单独覆盖保留。", buttons, message_id=message_id,
                         resume={"action": "policy_rule", "data": {"id": identifier}})

    async def save_rule(self, actor, draft, baseline, request_id):
        return await self.management.configuration.shared(actor, SharedPolicyPatch(
            rules=[draft], baseline={"rules": [baseline] if baseline else []}, request_id=request_id))

    async def prompt(self, actor, data):
        await self.management.principal(actor, root=True)
        await self.remember(f"bot_prompt:{actor}", {"group_id": None, "data": data, "expires": now() + 900})
        instruction = "请发送新的设置值。名单每行一项；发送 - 清空名单或可留空的文字。"
        if data["kind"] == "policy_rule_value":
            instruction = "请发送一条通用规则内容。域名填写主机名；推广目标可填写账号、链接、手机号或邮箱。"
        elif data["kind"] == "policy_rule_context":
            instruction = "请发送需要同时命中的正文关键词。原内容作为昵称关键词。"
        await self.send(actor, instruction + "\n发送 /cancel 取消；15 分钟内有效。")

    async def input(self, actor, text, data, request_id):
        await self.management.principal(actor, root=True)
        if data["kind"] == "policy_setting":
            key = data["key"]
            schema = GroupSettings.model_json_schema()["properties"][key]
            value = text
            if schema["type"] == "integer":
                if not text.isdecimal():
                    raise ManagementError(422, "请发送有效整数")
                value = int(text)
            elif schema["type"] == "array":
                value = [] if text == "-" else (re.split(r"[\s,，]+", text) if key.endswith("packs")
                                                   or key == "telegram_allowed_links" else text.splitlines())
            elif text == "-":
                value = ""
            await self.management.configuration.shared(actor, SharedPolicyPatch(settings={key: value},
                baseline={"settings": {key: data["baseline"]}}, request_id=request_id))
            await self.field(actor, key)
        else:
            baseline = data.get("baseline")
            draft = {**baseline} if baseline else {"kind": data["rule_kind"], "value": text}
            if data["kind"] == "policy_rule_context":
                draft.update(scope="name_and_message", context_value=text)
            else:
                draft["value"] = text
            await self.save_rule(actor, draft, baseline, request_id)
            await self.rules(actor)
        await self.remember(f"bot_prompt:{actor}", None)

    async def inheritance(self, actor, group_id, page=0, message_id=None):
        data = await self.management.configuration.inheritance(actor, group_id)
        keys = list(data["settings"])
        page = max(0, min(page, (len(keys) - 1) // 5))
        labels = admin_catalog()["settings"]["labels"]
        lines, buttons = ["本群设置来源与逐项恢复"], []
        for key in keys[page * 5:page * 5 + 5]:
            local = key in data["overrides"]
            lines.append(f"{labels[key]}：{shown(data['settings'][key])[:120]}（{'本群设置' if local else '跟随通用'}）")
            if local:
                lines.append("通用值：" + shown(data["defaults"][key])[:120])
                buttons.append(button("恢复 " + labels[key], "policy_reset", key=key,
                                      baseline=data["settings"][key], page=page))
        if page:
            buttons.append(button("上一页", "policy_inheritance", page=page - 1))
        if len(keys) > page * 5 + 5:
            buttons.append(button("下一页", "policy_inheritance", page=page + 1))
        buttons.append(button("返回群管理", "group", id=group_id))
        await self.panel(actor, "\n".join(lines), buttons, group_id, message_id,
                         resume={"action": "policy_inheritance", "data": {"page": page}})
