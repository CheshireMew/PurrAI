"""Operational readouts and explicit temporary-protection controls in Telegram."""

from purrai.bot_common import button, clock
from purrai.admin_labels import BACKUP_FIELDS, CHAT_STATES
from purrai.errors import ManagementError
from purrai.models import now
from purrai.schemas import BackupExerciseInput, BackupPlanInput, EmergencyInput


def backup_summary(plan):
    return "\n".join(f"{label}：{'开启' if value else '关闭'}" if isinstance(value, bool) else f"{label}：{value}"
                     for key, label in BACKUP_FIELDS.items() if (value := plan.get(key)) is not None)


def timing_summary(timing):
    if not timing:
        return "尚未记录"
    model = timing.get("model_seconds", "—")
    recent = timing.get("last_attempt_seconds", "—")
    usage = f"实际 {timing.get('actual_tokens', 0)} Token" if timing.get("token_usage_confirmed") else f"预留 {timing.get('reserved_tokens', 0)} Token，用量未确认"
    return f"模型 {model} 秒，最近处理 {recent} 秒；{usage}"


class OperationsPanels:
    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.remember = state.remember

    async def route(self, actor, group_id, action, data, request_id, message_id):
        if action == "ops_emergency":
            current = await self.management.protection.status(actor, group_id)
            text = (f"临时应急保护：{'正在启用' if current['active'] else '未启用'}\n"
                    + (f"结束：{clock(current.get('until', 0))}\n原因：{current.get('reason', '')}\n" if current['active'] else "")
                    + "临时要求新入群成员验证并拦截新人链接；结束后恢复原配置，已有验证和处罚保留。")
            return await self.panel(actor, text, [button("恢复日常保护" if current["active"] else "启用临时应急保护",
                "ops_emergency_prompt", active=not current["active"]), button("返回群管理", "group", id=group_id)], group_id, message_id)
        if action == "ops_emergency_prompt":
            await self.management.authorize(actor, group_id, write=True)
            return await self.prompt(actor, group_id, {"kind": "ops_emergency", "active": data["active"]}, "请发送启用或恢复的原因。输入后还需确认。")
        if action == "ops_emergency_commit":
            await self.management.protection.change(actor, group_id, EmergencyInput(**data, request_id=request_id))
            return await self.route(actor, group_id, "ops_emergency", {}, request_id, message_id)
        if action == "ops_chat":
            result = await self.management.chat_admin.diagnostics(actor, group_id, page_number=data.get("page", 0) + 1, size=5)
            lines = ["群聊天诊断 · 最近任务"]
            for row in result["items"]:
                lines.append(f"消息 {row['message_id']} · 话题 {row['topic_id']} · {CHAT_STATES.get(row['state'], '状态待核实')}\n{row['reason'] or '已按当前策略处理'}\n耗时：{timing_summary(row['timing'])}")
            lines.append("最近用量（北京时间）：\n" + "\n".join(f"{row['day']} · 话题 {row['topic_id']}：{row['calls']} 次 / {row['tokens']} Token 额度" for row in result["usage"][:8]))
            buttons = [button("返回聊天设置", "settings", section="chat")]
            page = data.get("page", 0)
            if page:
                buttons.insert(0, button("上一页", "ops_chat", page=page - 1))
            if result["total"] > page * 5 + 5:
                buttons.insert(0, button("下一页", "ops_chat", page=page + 1))
            return await self.panel(actor, "\n\n".join(lines), buttons, group_id, message_id)
        await self.management.principal(actor, root=True)
        if action == "ops_backup_exercise":
            result = await self.management.backups.exercise(actor, BackupExerciseInput(file=data["file"], request_id=request_id))
            return await self.send(actor, f"备份恢复演练：{result['file']}\n完整性：{'通过' if result['integrity_ok'] else '异常'}；外键：{'通过' if result['foreign_keys_ok'] else '异常'}；版本：{'与当前版本兼容' if result['compatible'] else '需配套版本或迁移'}\n未改写当前业务数据库。")
        if action == "ops_backup_prompt":
            current = (await self.management.backups.status(actor))["plan"]
            field = data.get("field")
            if field not in BACKUP_FIELDS:
                # Keep the existing batch-input boundary for already-open old prompts.
                await self.remember(f"bot_prompt:{actor}", {"group_id": None,
                    "data": {"kind": "ops_backup_plan"}, "expires": now() + 900})
                controls = [button(label, "ops_backup_prompt", field=key) for key, label in BACKUP_FIELDS.items()]
                controls.append(button("返回备份计划", "ops_backups"))
                return await self.panel(actor, "选择要修改的备份设置。修改后还需核对并确认，才会保存。",
                                       controls, message_id=message_id, resume={"action": "ops_backups", "data": {}})
            if isinstance(current[field], bool):
                if "value" in data:
                    return await self.preview_backup(actor, {field: data["value"]})
                return await self.panel(actor, BACKUP_FIELDS[field] + "：请选择开启或关闭。保存前还需确认。",
                    [button("开启", "ops_backup_prompt", field=field, value=True),
                     button("关闭", "ops_backup_prompt", field=field, value=False),
                     button("返回设置项目", "ops_backup_prompt")], message_id=message_id,
                    resume={"action": "ops_backups", "data": {}})
            schema = BackupPlanInput.model_json_schema()["properties"][field]
            return await self.prompt(actor, None, {"kind": "ops_backup_field", "field": field},
                f"请发送{BACKUP_FIELDS[field]}，填写 {schema['minimum']} 至 {schema['maximum']} 的整数。输入后还需确认。")
        if action == "ops_backup_commit":
            await self.management.backups.plan(actor, BackupPlanInput(**data, request_id=request_id))
        result = await self.management.backups.status(actor)
        page = max(0, data.get("page", 0))
        lines = ["备份计划与文件", backup_summary(result["plan"]),
                 "整理只作用于本计划创建且未被修改的旧文件；共享和外来备份保留。"]
        buttons = []
        for row in result["items"][page * 5:page * 5 + 5]:
            lines.append(f"{row['file']} · {row['bytes'] / 1048576:.2f} MB · {'本计划自有' if row['managed'] else '外来文件'}")
            buttons.append(button("恢复演练 · " + row["file"][7:22], "ops_backup_exercise", file=row["file"]))
        if page:
            buttons.append(button("上一页", "ops_backups", page=page - 1))
        if len(result["items"]) > page * 5 + 5:
            buttons.append(button("下一页", "ops_backups", page=page + 1))
        buttons += [button("修改计划与保留授权", "ops_backup_prompt"), button("返回系统状态", "system")]
        return await self.panel(actor, "\n".join(lines), buttons, message_id=message_id)

    async def prompt(self, actor, group_id, data, instruction):
        await self.remember(f"bot_prompt:{actor}", {"group_id": group_id, "data": data, "expires": now() + 900})
        return await self.send(actor, instruction + "\n发送 /cancel 取消。")

    async def input(self, actor, group_id, text, data):
        if data["kind"] == "ops_emergency":
            body = EmergencyInput(active=data["active"], reason=text.strip(), request_id="preview-only")
            buttons = [button("确认调整应急保护", "ops_emergency_commit", **body.model_dump(exclude={"request_id"})),
                       button("取消", "ops_emergency")]
            title = f"{'启用' if body.active else '恢复'} · 原因：{body.reason}"
        elif data["kind"] == "ops_backup_field":
            try:
                value = int(text.strip())
            except ValueError as exc:
                raise ManagementError(422, "请填写提示范围内的整数") from exc
            return await self.preview_backup(actor, {data["field"]: value})
        else:
            current = (await self.management.backups.status(actor))["plan"]
            changes = {}
            for line in text.strip().splitlines():
                key, equal, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if not equal or key not in current:
                    raise ManagementError(422, "请点击上方的备份设置项目，再按提示填写")
                if type(current[key]) is bool:
                    if value not in ("true", "false"):
                        raise ManagementError(422, "布尔值请填写 true 或 false")
                    changes[key] = value == "true"
                else:
                    try:
                        changes[key] = int(value)
                    except ValueError as exc:
                        raise ManagementError(422, "数值字段请填写整数") from exc
            return await self.preview_backup(actor, changes)
        await self.remember(f"bot_prompt:{actor}", None)
        return await self.panel(actor, title, buttons, group_id)

    async def preview_backup(self, actor, changes):
        await self.management.principal(actor, root=True)
        current = (await self.management.backups.status(actor))["plan"]
        plan = BackupPlanInput(**{**current, **changes}, request_id="preview-only")
        title = "核对备份计划：\n" + backup_summary(plan.model_dump())
        if plan.prune_enabled:
            title += "\n确认后允许按计划整理本程序登记且未被改动的旧备份，至少保留最新一份。"
        else:
            title += "\n自动整理关闭，不会按计划删除备份。"
        await self.remember(f"bot_prompt:{actor}", None)
        return await self.panel(actor, title,
            [button("确认计划与保留授权", "ops_backup_commit", **plan.model_dump(exclude={"request_id"})),
             button("取消", "ops_backups")], resume={"action": "ops_backups", "data": {}})
