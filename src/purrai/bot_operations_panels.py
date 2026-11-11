"""Operational readouts and explicit temporary-protection controls in Telegram."""

from purrai.bot_common import button, clock
from purrai.errors import ManagementError
from purrai.models import now
from purrai.schemas import BackupExerciseInput, BackupPlanInput, EmergencyInput


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
                lines.append(f"消息 {row['message_id']} · 话题 {row['topic_id']} · {row['state']}\n{row['reason'] or '已按当前策略处理'}\n耗时：{row['timing'] or '尚未记录'}")
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
            return await self.send(actor, f"备份恢复演练：{result['file']}\n完整性：{result['integrity_ok']}；外键：{result['foreign_keys_ok']}；版本兼容：{result['compatible']}\n未改写当前业务数据库。")
        if action == "ops_backup_prompt":
            return await self.prompt(actor, None, {"kind": "ops_backup_plan"},
                "请发送需要修改的计划字段，每行 key=value。布尔值用 true/false，其他字段填整数。\n"
                "enabled（计划开关）、hour_beijing（北京时间小时）、interval_days（间隔天数）、retention_days（保留天数）、retention_count（保留份数）、max_total_mb（总量 MB）、prune_enabled（允许整理本计划自有旧备份）、exercise_after_backup（备份后演练）。输入后还需确认授权。")
        if action == "ops_backup_commit":
            await self.management.backups.plan(actor, BackupPlanInput(**data, request_id=request_id))
        result = await self.management.backups.status(actor)
        page = max(0, data.get("page", 0))
        lines = ["备份计划与文件", *[f"{key} = {value}" for key, value in result["plan"].items()],
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
        else:
            current = (await self.management.backups.status(actor))["plan"]
            changes = {}
            for line in text.strip().splitlines():
                key, equal, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if not equal or key not in current:
                    raise ManagementError(422, "请逐行填写计划字段 key=value")
                if type(current[key]) is bool:
                    if value not in ("true", "false"):
                        raise ManagementError(422, "布尔值请填写 true 或 false")
                    changes[key] = value == "true"
                else:
                    try:
                        changes[key] = int(value)
                    except ValueError as exc:
                        raise ManagementError(422, "数值字段请填写整数") from exc
            plan = BackupPlanInput(**{**current, **changes}, request_id="preview-only")
            title = "核对备份计划：\n" + "\n".join(f"{key}={value}" for key, value in plan.model_dump(exclude={"request_id"}).items())
            title += "\nprune_enabled=true 即授权按计划整理本计划自有旧备份。"
            buttons = [button("确认计划与保留授权", "ops_backup_commit", **plan.model_dump(exclude={"request_id"})), button("取消", "ops_backups")]
        await self.remember(f"bot_prompt:{actor}", None)
        return await self.panel(actor, title, buttons, group_id)
