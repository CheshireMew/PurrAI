"""Telegram rule search and explicit false-positive correction review."""

from purrai.bot_common import button
from purrai.errors import ManagementError
from purrai.models import now, uid
from purrai.schemas import ViolationCorrectionInput


class ReviewPanels:
    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.remember, self.recall = state.remember, state.recall

    async def route(self, actor, action, group_id, data, request_id, message_id):
        await self.management.authorize(actor, group_id, write=True)
        if action == "review_search":
            return await self.search(actor, group_id, data.get("query", ""), data.get("page", 0), message_id)
        if action == "review_search_prompt":
            await self.remember(f"bot_prompt:{actor}", {"group_id": group_id,
                "data": {"kind": "review_search"}, "expires": now() + 900})
            return await self.send(actor, "请发送规则内容关键词；发送 - 查看全部。发送 /cancel 取消。")
        if action == "review_correction":
            evidence = await self.management.corrections.preview(actor, group_id, data["id"])
            draft_id = uid()
            draft = {"id": data["id"], "group_id": group_id, "actor": actor, "evidence": evidence,
                     "selected": [], "scope": "local", "action": "", "reason": "", "expires": now() + 900}
            await self.remember(f"bot_draft:correction:{draft_id}", draft)
        else:
            draft_id = data["draft_id"]
            draft = await self.load(actor, group_id, draft_id)
            if action == "review_toggle_rule":
                selected = set(draft["selected"])
                if data["id"] in selected:
                    selected.remove(data["id"])
                else:
                    selected.add(data["id"])
                draft["selected"] = sorted(selected)
            elif action == "review_scope":
                if data["scope"] == "global" and not draft["evidence"]["global_allowed"]:
                    raise ManagementError(403, "通用规则仅限所有者修改")
                draft["scope"] = data["scope"]
            elif action == "review_member_action":
                draft["action"] = data["operation"]
            elif action == "review_reason":
                await self.remember(f"bot_prompt:{actor}", {"group_id": group_id,
                    "data": {"kind": "review_reason", "draft_id": draft_id}, "expires": now() + 900})
                return await self.send(actor, "请发送核实原因（最多 300 字）。输入后返回确认面板，不会立即执行。")
            elif action == "review_commit":
                rules = draft["evidence"]["rules"]
                body = ViolationCorrectionInput(reason=draft["reason"], request_id=request_id,
                    action=draft["action"], scope=draft["scope"], rule_versions={item["id"]:
                    item["global_version"] if draft["scope"] == "global" else item["version"]
                    for item in rules if item["id"] in draft["selected"]})
                result = await self.management.corrections.apply(actor, group_id, draft["id"], body)
                if result["action_ids"]:
                    await self.management.track(actor, group_id, actor, result["action_ids"], request_id)
                await self.send(actor, "误判纠正已保存。解除限制任务以实际 Telegram 回执为准。")
                draft["expires"] = 0
                await self.remember(f"bot_draft:correction:{draft_id}", draft)
                return
            elif action != "review_show":
                raise ManagementError(422, "纠正操作不存在")
            await self.remember(f"bot_draft:correction:{draft_id}", draft)
        await self.show(actor, group_id, draft_id, draft, data.get("page", 0), message_id)

    async def load(self, actor, group_id, draft_id):
        draft = await self.recall(f"bot_draft:correction:{draft_id}")
        if not draft or draft["actor"] != actor or draft["group_id"] != group_id or draft["expires"] <= now():
            raise ManagementError(409, "纠正草稿已失效，请重新打开违规记录")
        return draft

    async def show(self, actor, group_id, draft_id, draft, page=0, message_id=None):
        rules = draft["evidence"]["rules"]
        page = max(0, min(page, max(0, (len(rules) - 1) // 5)))
        buttons = [button(f"{'●' if rule['id'] in draft['selected'] else '○'} 停用 {rule['value']}",
            "review_toggle_rule", draft_id=draft_id, id=rule["id"], page=page) for rule in rules[page * 5:page * 5 + 5]]
        if page:
            buttons.append(button("上一页", "review_show", draft_id=draft_id, page=page - 1))
        if len(rules) > page * 5 + 5:
            buttons.append(button("下一页", "review_show", draft_id=draft_id, page=page + 1))
        if draft["evidence"]["global_allowed"]:
            buttons.append(button("切换为只停用本群" if draft["scope"] == "global" else "切换为停用通用规则",
                "review_scope", draft_id=draft_id, scope="local" if draft["scope"] == "global" else "global"))
        record = await self.management.queries.detail(actor, group_id, "violation", draft["id"])
        for operation in record["allowed_operations"]:
            if operation in ("unmute", "unban"):
                buttons.append(button(("● " if draft["action"] == operation else "○ ")
                    + ("解除原禁言" if operation == "unmute" else "解除原封禁"), "review_member_action",
                    draft_id=draft_id, operation=operation))
        if draft["action"]:
            buttons.append(button("保留成员当前限制", "review_member_action", draft_id=draft_id, operation=""))
        buttons.append(button("填写核实原因", "review_reason", draft_id=draft_id))
        if draft["reason"]:
            buttons.append(button("确认保存纠正", "review_commit", draft_id=draft_id))
        buttons.append(button("返回违规记录", "detail", kind="violation", id=draft["id"]))
        impact = "只在本群停用勾选规则。"
        if draft["scope"] == "global":
            impact = "停用通用规则，影响群：" + "、".join(group["title"] for group in draft["evidence"]["affected_groups"]
                if set(group["rule_ids"]) & set(draft["selected"]))
        source_notes = "；".join(f"{rule['value']} 另有 {rule['other_sources']} 条来源" for rule in rules
                                if rule["id"] in draft["selected"])
        await self.panel(actor, f"纠正误判 · 用户 {record['user_id']}\n{record['excerpt'][:300]}\n"
            f"已选规则 {len(draft['selected'])} 条。{impact}\n{source_notes}\n"
            f"成员限制：{draft['action'] or '保留'}\n核实原因：{draft['reason'] or '尚未填写'}\n"
            "取消未执行的相关自动处罚，保留原证据；已删消息无法恢复。核对后点击确认保存。",
            buttons, group_id, message_id, resume={"action": "review_show", "data": {"draft_id": draft_id, "page": page}})

    async def search(self, actor, group_id, query="", page=0, message_id=None):
        result = await self.management.rule_queries.page(actor, group_id, query=query, page_number=page + 1, size=5)
        buttons = [button(f"{row['kind_label']} · {row['value']} · 命中 {row['hit_count']}", "detail", kind="rule", id=row["id"])
                   for row in result["items"]]
        if page:
            buttons.append(button("上一页", "review_search", query=query, page=page - 1))
        if result["total"] > (page + 1) * 5:
            buttons.append(button("下一页", "review_search", query=query, page=page + 1))
        buttons += [button("输入搜索词", "review_search_prompt"), button("返回全部规则", "list", kind="rule")]
        await self.panel(actor, f"搜索规则：{query or '全部'} · 共 {result['total']} 条", buttons, group_id, message_id,
                         resume={"action": "review_search", "data": {"query": query, "page": page}})

    async def input(self, actor, group_id, text, data):
        await self.management.authorize(actor, group_id, write=True)
        if data["kind"] == "review_search":
            await self.search(actor, group_id, "" if text == "-" else text[:250])
        else:
            draft = await self.load(actor, group_id, data["draft_id"])
            ViolationCorrectionInput(reason=text, request_id="validate-reason")
            draft["reason"] = text.strip()
            await self.remember(f"bot_draft:correction:{data['draft_id']}", draft)
            await self.show(actor, group_id, data["draft_id"], draft)
        await self.remember(f"bot_prompt:{actor}", None)
