"""Member summaries from the same read model as the web console."""

from purrai.errors import ManagementError

from purrai.admin_labels import OPERATIONS, STATE
from purrai.bot_common import button, clock


class MemberPanels:
    ACTIONS = ("members", "member_detail")
    INPUTS = ("member_lookup",)

    def __init__(self, management, ui):
        self.management, self.panel = management, ui.panel

    async def detail(self, actor, group_id, user_id, message_id=None):
        data = await self.management.member_queries.detail(actor, group_id, user_id)
        statuses = {"creator": "群主", "administrator": "管理员", "member": "正常成员",
                    "restricted": "受限制", "left": "已离群", "kicked": "被封禁"}
        current = data["current"] or {}
        text = f"成员 {user_id}\nTelegram 当前状态：{statuses.get(current.get('status'), '未核实')}\n"
        if data["current_error"]:
            text += data["current_error"] + "\n"
        member = data["member"]
        text += "记录的入群时间：" + (clock(member["joined_at"]) if member and member["join_observed"] else "无可靠记录") + "\n"
        white = data["whitelist"]
        text += (f"白名单：免验证{'是' if white['verification'] else '否'}，免自动处罚{'是' if white['moderation'] else '否'}\n"
                 if white else "未加入本群白名单\n")
        text += f"验证记录 {data['verifications']['total']} 条，违规记录 {data['violations']['total']} 条，操作记录 {data['actions']['total']} 条。\n"
        for item in data["actions"]["items"][:5]:
            text += f"\n{clock(item['created_at'])} {OPERATIONS.get(item['kind'], item['kind'])}：{STATE.get(item['state'], item['state'])}\n{item['reason']} {item['error']}"
        text += "\n禁言/封禁可发送 /mute 用户ID 30m 原因 或 /ban 用户ID 2h 原因；省略时长沿用群设置。"
        buttons = [button(OPERATIONS[action], "confirm", kind="member", user_id=user_id, operation=action)
                   for action in ("release", "reissue", "mute", "ban", "unmute", "unban")]
        buttons += [button("刷新成员状态", "member_detail", user_id=user_id), button("返回群管理", "group", id=group_id)]
        await self.panel(actor, text[:3900], buttons, group_id, message_id,
                         resume={"action": "member_detail", "data": {"user_id": user_id}})

    async def route(self, actor, group_id, action, data, request_id, message_id):
        if action == "members":
            await self.panel(
                actor,
                "选择成员操作。下一步输入用户 ID 并核对确认。",
                [
                    button(OPERATIONS[k], "prompt", kind="member", operation=k)
                    for k in ("release", "reissue", "mute", "ban", "unmute", "unban")
                ]
                + [button("返回群管理", "group", id=group_id)],
                group_id,
                message_id,
            )
        elif action == "member_detail":
            await self.detail(actor, group_id, data["user_id"], message_id)

    async def input(self, actor, group_id, text, data, request_id):
        kind = data["kind"]
        if kind == "member_lookup":
            if not text.isdecimal() or int(text) <= 0:
                raise ManagementError(422, "请发送成员的正数数字 ID")
            await self.detail(actor, group_id, int(text))
