"""Telegram panels for archive panels."""

from purrai.bot_common import button, clock
from purrai.schemas import (
    ArchiveFilter,
)


class ArchivePanels:
    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.tg, self.remember = ui.tg, state.remember

    async def archive_panel(self, actor, group_id, filters=None, page=0, message_id=None):
        filters = ArchiveFilter.model_validate(filters or {})
        result = await self.management.archived_messages(actor, group_id, filters, page + 1, 5)
        group = await self.management.authorize(actor, group_id)
        data = filters.model_dump()
        buttons = []
        for row in result["items"]:
            title = f"{clock(row['sent_at'])} · {row['sender_name'] or row['sender_id']} · {(row['text'] or row['caption'] or row['message_type'])[:20]}"
            buttons.append(button(title, "archive_detail", id=row["id"], filters=data, page=page))
        buttons += [
            button("全部消息", "archive", filters={}),
            button("只看命中规则", "archive", filters={**data, "flagged": not filters.flagged}),
            button(
                "只看确认删除", "archive", filters={**data, "deletion": "" if filters.deletion else "deleted"}
            ),
            button("按关键词搜索", "prompt", kind="archive_query", filters=data),
            button("按发送者 ID 筛选", "prompt", kind="archive_sender", filters=data),
            button("按北京时间范围筛选", "prompt", kind="archive_dates", filters=data),
            button("导出当前筛选 JSONL", "archive_export", filters=data),
            button(
                "关闭消息记录" if group.settings.get("archive_enabled", True) else "开启消息记录",
                "archive_toggle",
            ),
        ]
        if page:
            buttons.append(button("上一页", "archive", filters=data, page=page - 1))
        if result["total"] > (page + 1) * 5:
            buttons.append(button("下一页", "archive", filters=data, page=page + 1))
        buttons.append(button("返回群管理", "group", id=group_id))
        summary = f"关键词：{filters.query or '全部'}；发送者：{filters.sender_id or '全部'}\n命中规则：{'是' if filters.flagged else '不限'}；删除状态：{filters.deletion or '不限'}"
        if filters.after or filters.before:
            summary += f"\n时间：{clock(filters.after) if filters.after else '不限'} 至 {clock(filters.before) if filters.before else '不限'}"
        status = result["recording"]
        recording = {"active": "正在记录新消息", "disabled": "已停止记录新消息",
                     "capacity_paused": "容量不足，已暂停记录"}[status["state"]]
        if status["reason"]:
            recording += f"\n{status['reason']}"
        await self.panel(
            actor,
            f"{group.title} · 消息归档\n第 {page + 1} 页 · 共 {result['total']} 条\n"
            f"记录：{recording}\n{summary}\n"
            "保存收到的消息与编辑版本；关闭不会清除已有记录。Bot 每次最多导出 1000 条，网页可导出全部匹配消息。",
            buttons,
            group_id,
            message_id,
            resume={"action": "archive", "data": {"filters": data, "page": page}},
        )

    async def archive_detail(self, actor, group_id, data, message_id=None):
        row = await self.management.archived_message(actor, group_id, data["id"])
        states = {
            "not_requested": "未由 PurrAI 发起删除，是否仍在群内未知",
            "pending": "等待删除",
            "failed": "删除失败",
            "deleted": "PurrAI 已确认删除",
            "already_absent": "执行删除时已不存在",
        }
        text = (
            f"归档消息 {row['message_id']} · {clock(row['sent_at'])}\n"
            f"发送者：{row['sender_name']} @{row['username']} ({row['sender_id']})\n"
            f"类型：{row['message_type']} · 版本数：{row['revision_count']}\n"
            f"删除：{states[row['deletion_state']]}\n{row['deletion_error']}\n"
        )
        if row["source"] == "violation_excerpt":
            text += "旧版本仅有违规摘录，原始发送时间未知。\n"
        elif not row["original_observed"]:
            text += "首次收到时已经编辑，未收到原始版本。\n"
        for revision in row["revisions"][-3:]:
            body = (
                revision["payload"].get("text")
                or revision["payload"].get("caption")
                or "（无文字，媒体元数据见导出）"
            )
            text += f"\n{'编辑' if revision['is_edit'] else '收到'} · {clock(revision['event_at'])}\n{body[:650]}\n"
            if revision["reasons"]:
                text += "命中：" + "；".join(revision["reasons"])[:400] + "\n"
        if row["violation"]:
            text += "\n累计命中原因：" + "；".join(row["violation"]["reasons"])[:400]
        await self.panel(
            actor,
            text,
            [button("返回归档列表", "archive", filters=data.get("filters", {}), page=data.get("page", 0))],
            group_id,
            message_id,
        )
