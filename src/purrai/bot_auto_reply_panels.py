"""Private Telegram configuration for per-group keyword replies."""

from purrai.auto_reply_schemas import AutoReplyInput, AutoReplyPreview
from purrai.bot_common import button
from purrai.errors import ManagementError
from purrai.models import now


class AutoReplyPanels:
    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.remember = state.remember

    async def home(self, actor, group_id, page=0, message_id=None):
        result = await self.management.auto_replies.page(actor, group_id, page_number=page + 1, size=5)
        buttons = [button(f"{'启用' if row['enabled'] else '停用'} · {row['keyword'][:32]}",
                          "autoreply_detail", id=row["id"]) for row in result["items"]]
        buttons += [button("添加自动回复", "autoreply_new"), button("测试自动回复", "autoreply_test")]
        if page:
            buttons.append(button("上一页", "autoreply_home", page=page - 1))
        if result["total"] > (page + 1) * 5:
            buttons.append(button("下一页", "autoreply_home", page=page + 1))
        buttons.append(button("返回群管理", "group", id=group_id))
        await self.remember(f"bot_prompt:{actor}", None)
        await self.panel(actor, f"关键词自动回复 · 共 {result['total']} 条\n"
            "收到关键词后回复固定文字，无需 AI。每条消息只选优先级最高的一条；同优先级先添加的优先。\n"
            "冷却按本群的每条规则计算，0 秒表示不冷却。暂停群保护不影响自动回复。",
            buttons, group_id, message_id)

    async def detail(self, actor, group_id, identifier, message_id=None):
        row = await self.management.auto_replies.get(actor, group_id, identifier)
        data = {"id": identifier, "version": row["version"]}
        buttons = [button("修改关键词", "autoreply_keyword", **data), button("修改回复内容", "autoreply_reply", **data),
            button("改为包含匹配" if row["mode"] == "exact" else "改为完整匹配", "autoreply_mode", **data),
            button("停用" if row["enabled"] else "启用", "autoreply_toggle", **data),
            button("设置优先级", "autoreply_priority", **data), button("设置冷却时间", "autoreply_cooldown_seconds", **data),
            button("返回自动回复", "autoreply_home")]
        await self.panel(actor, f"关键词：{row['keyword']}\n"
            f"匹配：{'完整匹配' if row['mode'] == 'exact' else '包含匹配'}\n"
            f"优先级：{row['priority']}；冷却：{row['cooldown_seconds']} 秒\n"
            f"状态：{'启用' if row['enabled'] else '停用'}\n回复内容：\n{row['reply'][:1600]}"
            + ("\n（长回复可通过修改回复内容查看和替换）" if len(row["reply"]) > 1600 else ""),
            buttons, group_id, message_id)

    async def prompt(self, actor, group_id, data, instructions):
        await self.management.authorize(actor, group_id, write=True)
        await self.remember(f"bot_prompt:{actor}", {"group_id": group_id,
            "data": {"kind": "auto_reply", **data}, "expires": now() + 900})
        await self.send(actor, instructions + "\n发送 /cancel 取消；15 分钟内有效。")

    async def route(self, actor, group_id, action, data, request_id, message_id):
        await self.remember(f"bot_prompt:{actor}", None)
        if action == "autoreply_home":
            return await self.home(actor, group_id, data.get("page", 0), message_id)
        if action == "autoreply_detail":
            return await self.detail(actor, group_id, data["id"], message_id)
        if action == "autoreply_new":
            return await self.prompt(actor, group_id, {"field": "new_keyword"}, "请发送触发关键词，最多 250 个字符。")
        if action == "autoreply_test":
            return await self.prompt(actor, group_id, {"field": "test"}, "请发送测试消息，只预览回复，不会向群内发消息。")
        row = await self.management.auto_replies.get(actor, group_id, data["id"])
        if action in ("autoreply_toggle", "autoreply_mode"):
            changes = {"enabled": not row["enabled"]} if action == "autoreply_toggle" else {
                "mode": "contains" if row["mode"] == "exact" else "exact"}
            await self.management.auto_replies.save(actor, group_id, None, row["id"], request_id,
                data["version"], changes=changes)
            return await self.detail(actor, group_id, row["id"], message_id)
        field = action.removeprefix("autoreply_")
        instructions = {"keyword": "请发送新的触发关键词，最多 250 个字符。",
            "reply": "请发送完整的回复文字，最多 4000 个字符，支持换行，按纯文字发送。\n当前内容：\n" + row["reply"],
            "priority": "请发送优先级，0 至 1000，数值越大越优先。",
            "cooldown_seconds": "请发送冷却秒数，0 至 86400。0 表示不冷却。"}
        if field not in instructions:
            raise ManagementError(422, "自动回复操作不存在")
        # Send the current long reply separately so it is not truncated by panel text.
        if field == "reply":
            await self.send(actor, row["reply"])
            instructions[field] = "上方是当前完整回复。请发送新的完整回复文字，最多 4000 个字符，支持换行，按纯文字发送。"
        await self.prompt(actor, group_id, {"field": field, **data}, instructions[field])

    async def input(self, actor, group_id, text, data, request_id):
        field = data["field"]
        if field == "new_keyword":
            body = AutoReplyInput(keyword=text, reply=text)
            return await self.prompt(actor, group_id, {"field": "new_reply", "keyword": body.keyword},
                "请发送对应的完整回复文字，最多 4000 个字符，支持换行，按纯文字发送。")
        if field == "test":
            result = await self.management.auto_replies.preview(actor, group_id, AutoReplyPreview(text=text))
            await self.send(actor, "将回复以下内容（预览不计算冷却）：" if result["matched"] else "未命中启用的自动回复规则。")
            if result["matched"]:
                await self.send(actor, result["rule"]["reply"])
        elif field == "new_reply":
            row = await self.management.auto_replies.save(actor, group_id,
                AutoReplyInput(keyword=data["keyword"], reply=text), request_id=request_id)
            await self.remember(f"bot_prompt:{actor}", None)
            return await self.detail(actor, group_id, row["id"])
        else:
            value = text
            if field in ("priority", "cooldown_seconds"):
                if not text.isdecimal():
                    raise ManagementError(422, "请发送范围内的整数")
                value = int(text)
            await self.management.auto_replies.save(actor, group_id, None, data["id"], request_id,
                data["version"], changes={field: value})
            await self.remember(f"bot_prompt:{actor}", None)
            return await self.detail(actor, group_id, data["id"])
        await self.home(actor, group_id)
