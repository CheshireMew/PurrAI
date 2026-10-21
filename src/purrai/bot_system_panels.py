"""Telegram panels for system panels."""

from purrai.bot_common import button


class SystemPanels:
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
