"""Telegram panels for record panels."""

from purrai.admin_labels import KINDS, MODES, OPERATIONS, SCOPES, STATE
from purrai.bot_common import button, clock
from purrai.verification_state import ACTIVE


class RecordPanels:
    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.tg, self.remember = ui.tg, state.remember

    async def list_panel(self, actor, group_id, kind, page=0, message_id=None):
        rows, count = await self.management.records(actor, group_id, kind, page, pending=kind == "pending")
        group = await self.management.authorize(actor, group_id)
        names = {
            "pending": "待验证成员",
            "verification": "验证记录",
            "rule": "广告规则",
            "violation": "违规记录",
            "whitelist": "白名单",
            "action": "操作记录",
        }
        buttons, lines = [], [f"{group.title} · {names[kind]}\n第 {page + 1} 页 · 共 {count} 条"]
        for row in rows:
            identifier = row.get("id", row.get("user_id"))
            if kind in ("pending", "verification"):
                title = f"{row['user_name']} ({row['user_id']}) · {STATE.get(row['state'], row['state'])}"
            elif kind == "rule":
                title = f"{'启用' if row['enabled'] else '停用'} · {KINDS[row['kind']]} · {row['value']}"
            elif kind == "violation":
                title = f"用户 {row['user_id']} · {row['excerpt'][:25] or '联系人等消息'}"
            elif kind == "whitelist":
                title = f"{row['user_id']} · {row['note'] or '白名单成员'}"
            else:
                title = (
                    f"{OPERATIONS.get(row['kind'], row['kind'])} · {STATE.get(row['state'], row['state'])}"
                )
            buttons.append(button(title[:60], "detail", kind=kind, id=identifier))
        if not rows:
            lines.append("暂无记录。")
        if kind == "rule":
            buttons += [
                button("添加关键词", "prompt", kind="rule", rule_kind="keyword"),
                button("添加禁止域名", "prompt", kind="rule", rule_kind="block_domain"),
                button("添加允许域名", "prompt", kind="rule", rule_kind="allow_domain"),
                button("添加禁止引流账号", "prompt", kind="rule", rule_kind="block_mention"),
                button("添加允许提及账号", "prompt", kind="rule", rule_kind="allow_mention"),
                button("测试当前规则", "prompt", kind="preview"),
            ]
        if kind == "whitelist":
            buttons.append(button("添加白名单成员", "prompt", kind="whitelist"))
        if kind == "pending" and rows:
            buttons.append(button("批量放行所有待验证成员", "confirm", kind="batch"))
        if page:
            buttons.append(button("上一页", "list", kind=kind, page=page - 1))
        if count > page * 5 + 5:
            buttons.append(button("下一页", "list", kind=kind, page=page + 1))
        buttons.append(button("返回群管理", "group", id=group_id))
        await self.panel(
            actor,
            "\n".join(lines),
            buttons,
            group_id,
            message_id,
            resume={"action": "list", "data": {"kind": kind, "page": page}},
        )

    async def detail(self, actor, group_id, kind, identifier, message_id=None):
        row = await self.management.queries.single(actor, group_id, kind, identifier)
        buttons, text = [], ""
        if kind in ("pending", "verification"):
            text = (
                f"验证记录 · {row.user_name} ({row.user_id})\n状态：{STATE.get(row.state, row.state)}\n"
                f"截止：{clock(row.deadline)}（北京时间）\n错误次数：{row.attempts}\n{row.error}"
            )
            if row.failure_action:
                text += "\n实际失败处理：" + {
                    "permanent_mute": "永久禁言，留在群里",
                    "kick": "已踢出，可重新加入",
                    "decline": "拒绝入群申请",
                    "legacy_ban": "旧版或手动临时封禁",
                    "permanent_ban": "已踢出并禁止重新加入",
                }.get(row.failure_action, row.failure_action)
            choices = (
                ("release", "reissue", "ban")
                if row.state in ACTIVE
                else ("release", "reissue")
                if row.state == "failed" and row.failure_action == "permanent_mute"
                else ("unban",)
                if row.state == "failed" and row.failure_action in ("decline", "legacy_ban", "permanent_ban")
                else ()
            )
            buttons = [
                button(
                    OPERATIONS[action],
                    "confirm",
                    kind="verification",
                    id=row.id,
                    operation=action,
                    user_id=row.user_id,
                )
                for action in choices
            ]
        elif kind == "rule":
            body_penalty = "删消息并永久禁言" if row.penalty == "permanent_mute" else "只删消息"
            text = f"{KINDS[row.kind]}：{row.value}\n状态：{'启用' if row.enabled else '停用'}\n匹配：{MODES[row.mode]}\n范围：{SCOPES[row.scope]}\n处置：昵称违规删消息并永久禁言；正文违规{body_penalty}"
            if row.scope == "name_and_message":
                text += f"\n正文还需命中：{row.context_value}"
            buttons = [
                button("停用" if row.enabled else "启用", "rule_toggle", id=row.id, enabled=not row.enabled),
                button("修改内容", "prompt", kind="rule", id=row.id, rule_kind=row.kind),
            ]
            if row.kind == "keyword":
                buttons.append(
                    button(
                        "正文处置改为"
                        + ("只删消息" if row.penalty == "permanent_mute" else "删消息并永久禁言"),
                        "rule_penalty",
                        id=row.id,
                        penalty="delete" if row.penalty == "permanent_mute" else "permanent_mute",
                    )
                )
                buttons.append(
                    button(
                        "改为" + MODES[list(MODES)[(list(MODES).index(row.mode) + 1) % len(MODES)]] + "匹配",
                        "rule_mode",
                        id=row.id,
                        mode=list(MODES)[(list(MODES).index(row.mode) + 1) % len(MODES)],
                    )
                )
                scopes = ["message", "name", "either"]
                target = scopes[(scopes.index(row.scope) + 1) % 3] if row.scope in scopes else "message"
                buttons += [
                    button("范围改为" + SCOPES[target], "rule_scope", id=row.id, scope=target),
                    button("设置昵称＋正文组合", "prompt", kind="rule_context", id=row.id),
                ]
        elif kind == "violation":
            actions = await self.management.queries.violation_operations(actor, group_id, row.id)
            permanent_ban = any(operation.kind in ("contact_ban", "emoji_status_ban") for operation in actions)
            scope = "踢出并禁止重新加入 " if permanent_ban else (
                ("昵称或状态表情禁言 " if row.profile_violation else "")
                + ("正文广告永久禁言 " if row.rule_mute else "")
            )
            text = f"用户 {row.user_id} · {clock(row.created_at)}\n昵称：{row.sender_name}\n{row.excerpt[:600]}\n原因：{'；'.join(row.reasons)}\n处置范围：{scope}{'删除消息' if row.message_violation else ''}\n已删除的 Telegram 消息无法原地恢复。"
            for operation in actions:
                text += (
                    f"\n{OPERATIONS.get(operation.kind, operation.kind)}：{STATE.get(operation.state, operation.state)}"
                    + (f"（{operation.error}）" if operation.error else "")
                )
            if not row.sender_chat:
                choices = (
                    ("unban",)
                    if permanent_ban
                    else
                    ("revoke_warning", "unmute", "unban")
                    if row.warning_active
                    else ("unmute",)
                    if row.profile_violation or row.rule_mute
                    else ()
                )
                for action in choices:
                    if action != "revoke_warning" or row.warning_active:
                        buttons.append(
                            button(
                                OPERATIONS[action],
                                "confirm",
                                kind="violation",
                                id=row.id,
                                operation=action,
                                user_id=row.user_id,
                            )
                        )
        elif kind == "whitelist":
            text = f"白名单 · {row.user_id}\n备注：{row.note or '—'}\n免验证：{'是' if row.verification else '否'}\n免自动处罚：{'是' if row.moderation else '否'}"
            buttons = [
                button(
                    "切换免验证",
                    "white_toggle",
                    id=row.user_id,
                    key="verification",
                    value=not row.verification,
                ),
                button(
                    "切换免自动处罚",
                    "white_toggle",
                    id=row.user_id,
                    key="moderation",
                    value=not row.moderation,
                ),
                button("修改备注", "prompt", kind="white_note", id=row.user_id),
                button("移出白名单", "confirm", kind="white_remove", id=row.user_id),
            ]
        else:
            outcome = await self.management.queries.action(actor, row.id)
            text = f"{OPERATIONS.get(row.kind, row.kind)}\n状态：{STATE.get(outcome['state'], outcome['state'])}\n用户：{row.user_id or '—'}\n操作者：{row.actor_id or '系统'}\n原因：{row.reason or '—'}\n{outcome['error']}\n{clock(row.created_at)}（北京时间）"
            if outcome.get("summary"):
                counts = outcome["summary"]
                text += (
                    f"\n共 {counts['total']} 项：成功 {counts['succeeded']}，等待 {counts['pending']}，"
                    f"失败 {counts['failed']}，取消 {counts['cancelled']}"
                )
            result = row.payload.get("result", {})
            if row.kind == "admin":
                text += f"\n目标管理员：{result.get('id', '—')}\n群授权：{result.get('group_ids', [])}\n资格：{'有效' if result.get('active') else '已撤销'}"
            elif row.kind == "whitelist":
                text += f"\n目标成员：{result.get('user_id', '—')}"
            elif row.kind in ("retry", "retry_event"):
                text += f"\n目标记录：{result.get('event_id', result.get('action_id', '—'))}"
        buttons.append(button("返回列表", "list", kind=kind))
        await self.panel(
            actor,
            text,
            buttons,
            group_id,
            message_id,
            resume={"action": "detail", "data": {"kind": kind, "id": identifier}},
        )
