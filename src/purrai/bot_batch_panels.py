"""Paged, selectable Telegram batch release with a frozen review snapshot."""

from purrai.bot_common import button
from purrai.errors import ManagementError
from purrai.models import now, uid
from purrai.schemas import ReleaseSelectionInput, ReleasePendingInput


class BatchPanels:
    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.remember, self.recall = state.remember, state.recall

    async def route(self, actor, group_id, action, data, request_id, message_id):
        await self.management.authorize(actor, group_id, write=True)
        if action == "batch_review":
            preview = await self.management.members.release_preview(actor, group_id, ReleaseSelectionInput())
            if not preview["total"]:
                raise ManagementError(409, "当前没有待放行成员")
            identifier = uid()
            draft = {"actor": actor, "group_id": group_id, "items": preview["items"],
                     "selected": [row["id"] for row in preview["items"]], "reason": "", "expires": now() + 900}
        else:
            identifier = data["draft"]
            draft = await self.recall("bot_draft:batch:" + identifier)
            if not draft or draft["actor"] != actor or draft["group_id"] != group_id or draft["expires"] < now():
                raise ManagementError(409, "预览已过期，请重新查看待验证名单")
            if action == "batch_select":
                selected = set(draft["selected"])
                if data["id"] in selected:
                    selected.remove(data["id"])
                else:
                    selected.add(data["id"])
                draft["selected"] = sorted(selected)
            elif action == "batch_all":
                draft["selected"] = [row["id"] for row in draft["items"]] if data["select"] else []
            elif action == "batch_reason":
                await self.remember(f"bot_prompt:{actor}", {"group_id": group_id,
                    "data": {"kind": "batch_reason", "draft": identifier}, "expires": now() + 900})
                return await self.send(actor, "请填写本次批量放行原因，最多 300 字。随后还需确认执行。")
            elif action in ("batch_confirm", "batch_execute"):
                if not draft["selected"] or not draft["reason"]:
                    raise ManagementError(422, "请先勾选成员并填写放行原因")
                preview = await self.management.members.release_preview(actor, group_id,
                    ReleaseSelectionInput(verification_ids=draft["selected"]))
                if action == "batch_confirm":
                    # Compare with the reviewed records, not a newly substituted
                    # generation/nonce that the administrator never saw.
                    from purrai.release_selection import selection_snapshot
                    from types import SimpleNamespace
                    original = [SimpleNamespace(**row) for row in draft["items"] if row["id"] in draft["selected"]]
                    if selection_snapshot(original) != preview["snapshot"]:
                        raise ManagementError(409, "所选成员的验证已变化，请重新预览")
                    draft["snapshot"] = preview["snapshot"]
                    await self.remember("bot_draft:batch:" + identifier, draft)
                    return await self.panel(actor, f"确认放行所选 {len(draft['selected'])} 人？\n原因：{draft['reason']}\n仅提交已勾选的入群批次，后来入群的成员不包含在内。",
                        [button("确认执行", "batch_execute", draft=identifier), button("返回名单", "batch_page", draft=identifier)], group_id, message_id)
                result = await self.management.release_pending(actor, group_id, ReleasePendingInput(
                    action="release", request_id=request_id, reason=draft["reason"],
                    verification_ids=draft["selected"], snapshot=draft["snapshot"]))
                await self.management.track(actor, group_id, actor, result["action_ids"], request_id)
                return await self.send(actor, f"已提交所选 {len(draft['selected'])} 人，实际结果会在此反馈。")
        await self.remember("bot_draft:batch:" + identifier, draft)
        page = max(0, min(data.get("page", 0), (len(draft["items"]) - 1) // 8))
        buttons = [button(("✓ " if row["id"] in draft["selected"] else "○ ") + f"{row['user_name']} ({row['user_id']})"[:50],
                          "batch_select", draft=identifier, id=row["id"], page=page) for row in draft["items"][page * 8:page * 8 + 8]]
        if page:
            buttons.append(button("上一页", "batch_page", draft=identifier, page=page - 1))
        if page * 8 + 8 < len(draft["items"]):
            buttons.append(button("下一页", "batch_page", draft=identifier, page=page + 1))
        buttons += [button("全选预览名单", "batch_all", draft=identifier, select=True),
                    button("全部取消选择", "batch_all", draft=identifier, select=False),
                    button("填写放行原因", "batch_reason", draft=identifier),
                    button("核对并放行所选", "batch_confirm", draft=identifier),
                    button("返回待验证", "list", kind="pending")]
        await self.panel(actor, f"批量放行预览 · 第 {page + 1} 页\n已选 {len(draft['selected'])} / {len(draft['items'])} 人\n点击成员可切换选择。原因：{draft['reason'] or '尚未填写'}", buttons, group_id, message_id)

    async def input(self, actor, group_id, text, data):
        draft = await self.recall("bot_draft:batch:" + data["draft"])
        if not draft or draft["actor"] != actor or draft["group_id"] != group_id or draft["expires"] < now():
            raise ManagementError(409, "预览已过期，请重新查看名单")
        if not 1 <= len(text.strip()) <= 300:
            raise ManagementError(422, "请填写 1 至 300 字原因")
        draft["reason"] = text.strip()
        await self.remember("bot_draft:batch:" + data["draft"], draft)
        await self.remember(f"bot_prompt:{actor}", None)
        return await self.route(actor, group_id, "batch_page", data, "", None)
