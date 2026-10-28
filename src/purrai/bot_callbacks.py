"""Telegram callback ownership, expiry and panel continuity."""

from pydantic import ValidationError

from purrai.admin_labels import NAVIGATION_ACTIONS
from purrai.bot_common import explain
from purrai.errors import ManagementError
from purrai.models import now


class BotCallbacks:
    def __init__(self, ui, state, routes, inputs):
        self.send, self.acknowledge, self.recall = ui.send, ui.acknowledge, state.recall
        self.state = state
        self.routes, self.inputs = routes, inputs

    @staticmethod
    def legacy_resume(record):
        action, group_id, data = record["action"], record["group_id"], record["data"]
        if action in ("chat_toggle", "chat_reset"):
            return {"action": "settings", "data": {"section": "chat"}}
        if action in ("toggle", "failure_policy"):
            return {"action": "settings", "data": {"section": data.get("section", "verification")}}
        if action in ("archive_toggle", "archive_export"):
            return {"action": "archive", "data": {"filters": data.get("filters", {})}}
        if action in ("rule_toggle", "rule_mode", "rule_scope", "rule_penalty", "white_toggle"):
            return {
                "action": "detail",
                "data": {"kind": "whitelist" if action == "white_toggle" else "rule", "id": data["id"]},
            }
        if action == "emoji_pack_remove":
            return {"action": "emoji_packs", "data": {"page": data.get("page", 0)}}
        if action == "group_access":
            return {"action": "access_panel", "data": {"id": data["id"]}}
        if action == "retry":
            return {"action": "failures", "data": {"event": data.get("event", False)}}
        if action == "backup":
            return {"action": "system", "data": {}}
        return (
            {"action": "group", "data": {"id": group_id}}
            if group_id is not None
            else {"action": "groups", "data": {}}
        )

    async def resume_panel(self, actor, token, record, message_id):
        if record["action"] == "execute":
            return await self.inputs.confirmation(actor, record["group_id"], record["data"], message_id)
        if record["action"] == "emoji_packs_clear":
            resume = {"action": "emoji_packs_clear_confirm", "data": {}}
        else:
            resume = record.get("resume") or self.legacy_resume(record)
        await self.routes.route(actor, token, {**record, **resume}, message_id)

    async def callback(self, query):
        if not query.get("data", "").startswith("a:"):
            return False
        actor = query["from"]["id"]
        message = query.get("message", {})
        try:
            if message.get("chat", {}).get("type") != "private" or message["chat"]["id"] != actor:
                raise ManagementError(403, "请在与机器人的私聊中操作管理面板")
            token = query["data"][2:]
            record = await self.recall(f"bot_button:{token}")
            if not record:
                group_id = await self.recall(f"bot_selected:{actor}")
                resume = self.legacy_resume({"action": "missing", "group_id": group_id, "data": {}})
                await self.acknowledge(query["id"], "正在恢复管理面板")
                await self.routes.route(
                    actor, token, {**resume, "group_id": group_id}, message.get("message_id")
                )
                return True
            if record["actor"] != actor:
                raise ManagementError(403, "此按钮属于其他管理员，请使用你自己的管理面板")
            mutation = record["action"] not in NAVIGATION_ACTIONS
            current = await self.recall(f"bot_panel:{actor}:{message.get('message_id')}")
            if mutation and (
                not record.get("panel") or record.get("processed")
                or current and record.get("panel") != current["panel"]
            ):
                await self.acknowledge(query["id"], "面板已更新，本次未重复执行，请使用当前按钮")
                resume = current or {
                    "group_id": record["group_id"],
                    "resume": record.get("resume") or self.legacy_resume(record),
                }
                await self.routes.route(actor, token, {"group_id": resume["group_id"], **resume["resume"]},
                                        message.get("message_id"))
                return True
            expires = record.get("expires")
            if record["action"] not in NAVIGATION_ACTIONS and expires is not None and expires <= now():
                await self.acknowledge(query["id"], "已接续面板，请核对后再次选择要执行的操作")
                await self.resume_panel(actor, token, record, message.get("message_id"))
                return True
            await self.acknowledge(query["id"], "")
            await self.routes.route(actor, token, record, message.get("message_id"))
            if mutation:
                await self.state.complete_button(token)
        except (ManagementError, ValidationError) as exc:
            await self.acknowledge(query["id"], "操作未完成")
            await self.send(actor, explain(exc))
        return True
