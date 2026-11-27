"""Telegram panels for system panels."""

from purrai.errors import ManagementError
from purrai.schemas import DeliveryResolutionInput

from purrai.bot_common import button
from purrai.action_delivery import uncertain_delivery
from purrai.models import now


class SystemPanels:
    ACTIONS = ("system", "failures", "retry", "delivery_resolution", "backup")
    INPUTS = ("delivery_resolution",)

    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.tg, self.remember = ui.tg, state.remember

    async def system(self, actor, message_id=None):
        status = await self.management.queries.system(actor)
        pending, failed = status["pending_actions"], status["failed_count"]
        backup, archive_status, budget = (
            status["last_backup"],
            status["archive_status"],
            status["chat_budget"],
        )
        await self.panel(
            actor,
            f"PurrAI 系统状态\nTelegram：{'已连接' if self.tg.connected else '未连接'}\n待执行任务：{pending}\n失败任务：{failed}\n最近备份：{backup or '尚未生成'}\n"
            f"今日模型请求：{budget['global_calls']}/{budget['global_call_limit']}；Token 预算：{budget['global_tokens']}/{budget['global_token_limit']}\n"
            + (f"归档：{archive_status}\n" if archive_status else "")
            + "网页后台为可选入口，Telegram 管理不需要配置 OIDC。",
            [
                button("查看失败任务", "failures", event=False),
                button("查看失败事件", "failures", event=True),
                button("生成一致性备份", "backup"),
                button("备份计划、文件与演练", "ops_backups"),
                button("刷新", "system"),
                button("返回选群", "groups"),
            ],
            message_id=message_id,
            resume={"action": "system", "data": {}},
        )

    async def failures(self, actor, event=False, page=0, message_id=None):
        rows = await self.management.queries.failures(actor, event=event, page=page)
        text = "失败事件" if event else "失败任务"
        buttons = []
        for row in rows[:5]:
            text += f"\n\n{row.id}\n{row.error or '暂无错误说明'}"
            buttons.append(
                button(
                    f"重试 · {row.id[:8] if isinstance(row.id, str) else row.id}",
                    "retry",
                    id=row.id,
                    event=event,
                )
            )
            if not event and (uncertain_delivery(row.payload) or row.payload.get("challenge_delivery") in ("sending", "uncertain")):
                buttons.append(button(f"核实 · {row.id[:8]}", "delivery_resolution", id=row.id))
        if not rows:
            text += "\n暂无失败记录。"
        if page:
            buttons.append(button("上一页", "failures", event=event, page=page - 1))
        if len(rows) > 5:
            buttons.append(button("下一页", "failures", event=event, page=page + 1))
        buttons.append(button("返回系统状态", "system"))
        await self.panel(
            actor,
            text,
            buttons,
            message_id=message_id,
            resume={"action": "failures", "data": {"event": event, "page": page}},
        )

    async def resolution_prompt(self, actor, identifier):
        await self.management.principal(actor, root=True)
        await self.remember(f"bot_prompt:{actor}", {"group_id": None,
            "data": {"kind": "delivery_resolution", "id": identifier}, "expires": now() + 900})
        await self.send(actor, "请先在 Telegram 核实，再发送：已完成 核实说明，或 未完成 核实说明。\n"
            "仅登记人工结果并移出待处理失败列表，不自动重发、不改写原始错误或成员状态。发送 /cancel 取消。")

    async def route(self, actor, group_id, action, data, request_id, message_id):
        if action == "system":
            await self.system(actor, message_id)
        elif action == "failures":
            await self.failures(actor, data.get("event", False), data.get("page", 0), message_id)
        elif action == "retry":
            event = data.get("event", False)
            await self.management.retry(actor, data["id"], event, request_id)
            if not event:
                row = await self.management.queries.action(actor, data["id"])
                if row["group_id"]:
                    await self.management.track(actor, row["group_id"], actor, [row["id"]], request_id)
            await self.send(actor, "重试已受理。可返回系统状态查看实际处理结果。")
            await self.failures(actor, event, message_id=message_id)
        elif action == "delivery_resolution":
            await self.resolution_prompt(actor, data["id"])
        elif action == "backup":
            result = await self.management.backup(actor, request_id)
            await self.send(actor, f"一致性备份已保存：{result['file']}")

    async def input(self, actor, group_id, text, data, request_id):
        kind = data["kind"]
        if kind == "delivery_resolution":
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or parts[0] not in ("已完成", "未完成"):
                raise ManagementError(422, "请发送：已完成 核实说明，或 未完成 核实说明")
            await self.management.system_admin.resolve_delivery(
                actor,
                data["id"],
                DeliveryResolutionInput(
                    request_id=request_id,
                    outcome="completed" if parts[0] == "已完成" else "not_completed",
                    reason=parts[1],
                ),
            )
            await self.remember(f"bot_prompt:{actor}", None)
            await self.send(actor, "人工核实结果已登记，未自动重发；可在操作记录查看。")
            return
