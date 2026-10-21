"""Telegram panels for group panels."""

from purrai.admin_labels import STATE
from purrai.bot_common import button
from purrai.errors import ManagementError


class GroupPanels:
    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.tg, self.remember = ui.tg, state.remember

    async def choose_groups(self, actor, page=0, message_id=None):
        groups = await self.management.groups(actor)
        if not groups and actor not in self.management.settings.superadmin_ids:
            raise ManagementError(403, "你目前不是机器人已加入群的管理员，请先在 Telegram 群中设置管理员身份")
        await self.remember(f"bot_prompt:{actor}", None)
        buttons = [
            button(
                f"{STATE.get(g['status'], g['status'])} · {g['title']}",
                "group" if g["approved"] else "access_panel",
                id=g["id"],
            )
            for g in groups[page * 5 : page * 5 + 5]
        ]
        if page:
            buttons.append(button("上一页", "groups", page=page - 1))
        if len(groups) > page * 5 + 5:
            buttons.append(button("下一页", "groups", page=page + 1))
        if await self.management.principal(actor):
            buttons.append(button("系统状态与备份", "system"))
        buttons.append(button("权限与账号说明", "admins"))
        await self.panel(
            actor,
            "PurrAI · 管理面板\n请选择要管理的群。\n"
            + (
                "还没有可管理群，请先把机器人加入超级群。"
                if not groups
                else "可以直接在 Telegram 完成配置和处置。"
            ),
            buttons,
            message_id=message_id,
            resume={"action": "groups", "data": {"page": page}},
        )

    async def group_panel(self, actor, group_id, message_id=None):
        group = await self.management.authorize(actor, group_id)
        await self.remember(f"bot_selected:{actor}", group_id)
        await self.remember(f"bot_prompt:{actor}", None)
        buttons = [button("检查机器人权限", "permissions")]
        if group.enabled:
            buttons.append(button("暂停群保护", "pause"))
        else:
            buttons.append(button("检查并启用群保护", "activate"))
        buttons += [
            button("验证设置", "settings", section="verification"),
            button("防广告设置", "settings", section="moderation"),
            button("群聊天设置", "settings", section="chat"),
            button("待验证成员", "list", kind="pending"),
            button("全部验证记录", "list", kind="verification"),
            button("广告规则与测试", "list", kind="rule"),
            button("违规记录", "list", kind="violation"),
            button("消息归档与导出", "archive"),
            button("白名单", "list", kind="whitelist"),
            button("成员放行 / 禁言 / 封禁", "members"),
            button("操作记录", "list", kind="action"),
            button("切换群", "groups"),
        ]
        if group.settings.get("join_requests"):
            buttons.insert(1, button("生成申请入群链接", "join_link"))
        if actor in self.management.settings.superadmin_ids:
            buttons.append(button("接入授权", "access_panel", id=group_id))
        text = f"PurrAI · {group.title}\n群 ID：{group_id}\n状态：{STATE.get(group.status, group.status)}\n"
        text += "暂停保护不会自动放行已经开始验证的成员，可在待验证列表批量放行。"
        if group.settings.get("join_requests"):
            text += "\n申请模式：请使用需要审核的入群链接；直接入群仍会执行群内验证。"
        await self.panel(actor, text, buttons, group_id, message_id)

    async def access_panel(self, actor, group_id, message_id=None):
        group = await self.management.queries.pending_group(actor, group_id)
        await self.panel(
            actor,
            f"仅限自用和朋友群 · {group.title}\n群 ID：{group_id}\n"
            + (
                "此群已获准接入。撤销后停止聊天和管理任务，已有禁言不会自动解除。"
                if group.approved
                else "此群尚未获准接入，不记录群消息、不调用模型。批准后，本群管理员可自行配置。"
            ),
            [
                button(
                    "撤销接入" if group.approved else "批准此群接入",
                    "group_access",
                    id=group_id,
                    approved=not group.approved,
                ),
                button("返回选群", "groups"),
            ],
            message_id=message_id,
        )

    async def admins(self, actor, message_id=None, page=0):
        owner = await self.management.principal(actor)
        text = (
            f"权限说明\n你的 Telegram 数字 ID：{actor}\n"
            "仅允许所有者批准的自用和朋友群；获准群的群主和管理员自动获得本群管理权限。\n"
            "新增或撤销管理员，请直接在 Telegram 群设置中操作。\n"
            "超级管理员负责全局维护，由服务器配置指定。"
        )
        if owner:
            text += "\n超级管理员 ID：" + "、".join(map(str, self.management.settings.superadmin_ids))
        await self.panel(actor, text, [button("返回选群", "groups")], message_id=message_id)
