"""Private Telegram command/button administration. No model or web login is required."""

import json
import secrets
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from pydantic import ValidationError
from sqlalchemy import func, select

from purrai.emoji_status import DEFAULT_BLOCKED_PACKS
from purrai.management import ManagementError
from purrai.moderation_records import violation_actions
from purrai.models import Action, Group, Inbox, Meta, Rule, Verification, Violation, Whitelist, now
from purrai.schemas import (
    ActionInput,
    ArchiveFilter,
    EmojiPackChange,
    GroupSettings,
    PreviewInput,
    RuleInput,
    SettingsPatch,
    WhitelistInput,
)
from purrai.service import ACTIVE, row_dict


STATE = {
    "unapproved": "等待所有者批准",
    "pending": "等待处理",
    "active": "保护中",
    "paused": "已暂停",
    "error": "需要处理",
    "muting": "正在禁言",
    "releasing": "等待恢复发言",
    "removing": "正在移出",
    "failing": "正在执行失败处理",
    "passed": "已通过",
    "failed": "失败",
    "succeeded": "已完成",
    "cancelled": "已取消",
    "left": "已退群",
    "superseded": "已换新验证",
}
OPERATIONS = {
    "release": "放行",
    "reissue": "重新出题",
    "ban": "临时封禁",
    "mute": "临时禁言",
    "unmute": "解除 PurrAI 禁言",
    "unban": "解除 PurrAI 封禁",
    "revoke_warning": "撤销警告",
    "manual_mute": "手动禁言",
    "manual_ban": "手动封禁",
    "challenge": "重新出题",
    "settings": "修改配置",
    "rule": "修改规则",
    "whitelist": "白名单",
    "activate": "启用保护",
    "verify_mute": "验证禁言",
    "moderation_mute": "违规禁言",
    "delete": "删除消息",
    "warn": "群内通知",
    "progress": "更新验证输入",
    "batch_release": "批量放行",
    "admin_notify": "执行结果回执",
    "admin": "管理员授权",
    "backup": "生成备份",
    "retry": "重试任务",
    "retry_event": "重试事件",
    "profile_mute": "昵称命中永久禁言",
    "emoji_status_check": "检查名字旁状态表情",
    "emoji_status_mute": "状态表情命中永久禁言",
    "join_link": "生成申请入群链接",
    "verification_failure": "验证失败处理",
    "archive_export": "导出消息归档",
}
BOOLEANS = {
    "archive_enabled": "记录群消息归档",
    "join_requests": "申请入群先验证",
    "verification_enabled": "图片验证",
    "newcomer_links": "新成员限制链接",
    "newcomer_contacts": "新成员限制联系人",
    "newcomer_mentions": "新成员限制 @引流",
    "repeat_enabled": "重复刷屏检测",
    "block_invites": "群邀请链接拦截",
    "similar_enabled": "近似内容刷屏检测",
    "burst_enabled": "连续发送检测",
}
NUMBERS = {
    "verification_timeout": "验证时限（秒）",
    "max_attempts": "最多错误次数",
    "failure_ban_seconds": "手动封禁及申请重试间隔（秒）",
    "newcomer_seconds": "新成员限制期（秒）",
    "newcomer_messages": "前几条消息继续观察（0 关闭）",
    "media_min_text_messages": "发图及表情前独立文字条数（0 关闭）",
    "repeat_window": "刷屏检测窗口（秒）",
    "repeat_count": "刷屏触发条数",
    "similar_window": "近似内容窗口（秒）",
    "similar_count": "近似内容触发条数",
    "similar_threshold": "近似程度（百分比）",
    "similar_min_length": "近似检测最短文字长度",
    "burst_window": "连续发送窗口（秒）",
    "burst_count": "连续发送触发条数",
    "mute_seconds": "手动禁言（秒）",
}
KINDS = {
    "keyword": "关键词",
    "block_domain": "禁止域名",
    "allow_domain": "允许域名",
    "block_mention": "禁止引流账号",
    "allow_mention": "允许提及账号",
}
MODES = {"contains": "包含", "exact": "完整", "compact": "忽略表情和分隔符", "pinyin": "谐音"}
SCOPES = {
    "message": "正文",
    "name": "昵称及用户名",
    "either": "昵称或正文",
    "name_and_message": "昵称＋正文组合",
}
CHAT_FIELDS = {
    "chat_name": "聊天昵称",
    "chat_persona": "性格与说话习惯",
    "chat_history": "最近消息条数（10 至 80）",
    "chat_interval": "主动接话检查间隔（15 至 3600 秒）",
    "chat_sticker_packs": "聊天贴纸包（addstickers 链接或短名称，多个用空格或换行分隔，- 清空）",
    "chat_sticker_interval": "聊天贴纸最短间隔（15 至 86400 秒）",
    "chat_antifraud_packs": "防诈贴纸包（addstickers 链接或短名称，多个用空格或换行分隔，- 清空）",
    "chat_antifraud_interval": "防诈提醒最短间隔（3600 至 604800 秒）",
}
CHAT_BUTTON_LABELS = {
    "chat_name": "聊天昵称",
    "chat_persona": "性格与说话习惯",
    "chat_history": "最近消息条数",
    "chat_interval": "主动接话检查间隔",
    "chat_sticker_packs": "聊天贴纸包",
    "chat_sticker_interval": "聊天贴纸最短间隔",
    "chat_antifraud_packs": "防诈贴纸包",
    "chat_antifraud_interval": "防诈提醒最短间隔",
}
# Navigation is durable. Execution buttons retain a short lifetime and reopen
# their current screen when stale instead of abandoning the administration flow.
NAVIGATION_ACTIONS = {
    "groups", "group", "access_panel", "settings", "archive", "archive_detail",
    "list", "detail", "members", "emoji_packs", "admins", "system", "failures",
    "prompt", "confirm", "emoji_packs_clear_confirm", "permissions",
}


def keyboard_rows(buttons):
    if len(buttons) < 6:
        return [[item] for item in buttons]
    rows, pair = [], []
    for item in buttons:
        label = item["text"]
        width = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in label)
        if width > 32 or label.startswith(("返回", "取消", "切换群")):
            if pair:
                rows.append(pair)
                pair = []
            rows.append([item])
        else:
            pair.append(item)
            if len(pair) == 2:
                rows.append(pair)
                pair = []
    if pair:
        rows.append(pair)
    return rows


def clock(value):
    return (
        datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S") if value else "—"
    )


def button(label, action, **data):
    return label, action, data


class BotAdmin:
    def __init__(self, management):
        self.management = management
        self.db, self.tg = management.db, management.tg

    async def remember(self, key, value):
        async with self.db.transaction() as session:
            row = await session.get(Meta, key)
            encoded = json.dumps(value, ensure_ascii=False)
            if row:
                row.value = encoded
            else:
                session.add(Meta(key=key, value=encoded))

    async def recall(self, key):
        async with self.db.sessions() as session:
            row = await session.get(Meta, key)
            return json.loads(row.value) if row else None

    async def send(self, chat_id, text):
        return await self.tg.call("send_message", chat_id=chat_id, text=text[:4000])

    async def panel(self, actor, text, buttons, group_id=None, message_id=None, *, resume=None):
        keyboard = []
        resume = resume or ({"action": "group", "data": {"id": group_id}} if group_id is not None
                            else {"action": "groups", "data": {}})
        async with self.db.transaction() as session:
            for label, action, data in buttons:
                token = secrets.token_hex(8)
                session.add(
                    Meta(
                        key=f"bot_button:{token}",
                        value=json.dumps(
                            {
                                "actor": actor,
                                "group_id": group_id,
                                "action": action,
                                "data": data,
                                "expires": None if action in NAVIGATION_ACTIONS else now() + 900,
                                "resume": resume,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                keyboard.append({"text": label[:60], "callback_data": f"a:{token}"})
        markup = {"inline_keyboard": keyboard_rows(keyboard)}
        if message_id:
            try:
                await self.tg.call(
                    "edit_message_text",
                    chat_id=actor,
                    message_id=message_id,
                    text=text[:4000],
                    reply_markup=markup,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
        await self.tg.call("send_message", chat_id=actor, text=text[:4000], reply_markup=markup)

    async def choose_groups(self, actor, page=0, message_id=None):
        groups = await self.management.groups(actor)
        await self.remember(f"bot_prompt:{actor}", None)
        buttons = [
            button(f"{STATE.get(g['status'], g['status'])} · {g['title']}", "group" if g["approved"] else "access_panel", id=g["id"])
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
        await self.management.principal(actor, root=True)
        async with self.db.sessions() as session:
            group = await session.get(Group, group_id)
        if not group:
            raise ManagementError(404, "群不存在")
        await self.panel(actor,
            f"仅限自用和朋友群 · {group.title}\n群 ID：{group_id}\n"
            + ("此群已获准接入。撤销后停止聊天和管理任务，已有禁言不会自动解除。"
               if group.approved else "此群尚未获准接入，不记录群消息、不调用模型。批准后，本群管理员可自行配置。"),
            [button("撤销接入" if group.approved else "批准此群接入", "group_access", id=group_id, approved=not group.approved),
             button("返回选群", "groups")], message_id=message_id)

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
        await self.panel(
            actor,
            f"{group.title} · 消息归档\n第 {page + 1} 页 · 共 {result['total']} 条\n"
            f"记录：{'开启' if group.settings.get('archive_enabled', True) else '关闭'}\n{summary}\n"
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

    async def chat_panel(self, actor, group_id, message_id=None):
        group = await self.management.authorize(actor, group_id)
        cfg = GroupSettings.model_validate(group.settings)
        status = await self.management.chat_status(actor, group_id)
        text = (
            f"{group.title} · 群聊天设置\n"
            f"模型：{status['model'] if status['configured'] else '尚未配置；请在服务器填写聊天 API 地址、Key 和模型名'}\n"
            f"昵称：{cfg.chat_name}\n性格：{cfg.chat_persona or '自然随和'}\n"
            f"最近上下文：{cfg.chat_history} 条，同群不同话题分开\n"
            f"主动接话最多每 {cfg.chat_interval} 秒检查一次；模型觉得不适合时会保持安静。\n"
            f"聊天贴纸：{len(cfg.chat_sticker_packs)} 包，至少间隔 {cfg.chat_sticker_interval} 秒\n"
            f"防诈提醒：至少间隔 {cfg.chat_antifraud_interval // 3600} 小时，白天且群内有新讨论时才发\n"
            f"贴纸列表：已加载 {status.get('stickers', {}).get('ready', 0)}/{status.get('stickers', {}).get('total', 0)} 包\n"
            f"聊天 emoji 标签：{status.get('stickers', {}).get('emoji_count', 0)} 种，自动包含各包全部标签\n"
            "@机器人、叫昵称或回复它都可以开始聊；最近正在聊的群友可以自然接着说。\n"
            f"待处理：{status['pending']}\n" + (f"最近问题：{status['error']}\n" if status["error"] else "")
        )
        budget = status.get("budget", {})
        refreshed = datetime.fromtimestamp(now(), ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        text += (f"统计刷新：{refreshed}（北京时间；点击刷新才更新）\n"
                 f"本群今日累计请求：已用 {budget.get('group_calls', 0)} 次 / 上限 {budget.get('group_call_limit', '—')} 次\n"
                 f"本群今日 Token 额度占用：{budget.get('group_tokens', 0):,} / {budget.get('group_token_limit', 0):,}\n"
                 "统计包含全群所有成员和话题，保持安静和重试也计数。\n"
                 "Token 含历史上下文及请求预留，按实际用量结算后可能回落；不是单条消息用量。\n"
                 "每日额度由所有者设置，北京时间零点重置；验证和防广告不占此额度。")
        buttons = [
            button(
                f"{'已开启' if cfg.chat_enabled else '已关闭'} · 群聊天",
                "chat_toggle",
                key="chat_enabled",
                value=not cfg.chat_enabled,
            ),
            button(
                f"{'已开启' if cfg.chat_proactive else '已关闭'} · 主动参与讨论",
                "chat_toggle",
                key="chat_proactive",
                value=not cfg.chat_proactive,
            ),
        ]
        buttons += [
            button(f"{'已开启' if cfg.chat_stickers else '已关闭'} · 聊天使用贴纸", "chat_toggle",
                   key="chat_stickers", value=not cfg.chat_stickers),
            button(f"{'已开启' if cfg.chat_antifraud else '已关闭'} · 偶尔发防诈贴纸", "chat_toggle",
                   key="chat_antifraud", value=not cfg.chat_antifraud),
        ]
        if status.get("stickers", {}).get("unavailable"):
            text += "暂时不可用：" + "、".join(status["stickers"]["unavailable"]) + "\n"
        if status.get("stickers", {}).get("error"):
            text += status["stickers"]["error"] + "\n"
        buttons += [
            button(CHAT_BUTTON_LABELS[key], "prompt", kind="chat_setting", key=key) for key in CHAT_FIELDS
        ]
        buttons += [
            button("重新开始聊天上下文", "chat_reset"),
            button("刷新聊天状态", "settings", section="chat"),
            button("返回群管理", "group", id=group_id),
        ]
        await self.panel(actor, text, buttons, group_id, message_id,
                         resume={"action": "settings", "data": {"section": "chat"}})

    async def settings_panel(self, actor, group_id, section, message_id=None):
        if section == "chat":
            return await self.chat_panel(actor, group_id, message_id)
        group = await self.management.authorize(actor, group_id)
        keys = (
            [
                "join_requests",
                "verification_enabled",
                "verification_timeout",
                "max_attempts",
            ]
            if section == "verification"
            else [
                key
                for key in [*BOOLEANS, *NUMBERS]
                if key
                not in (
                    "join_requests",
                    "verification_enabled",
                    "verification_timeout",
                    "max_attempts",
                )
            ]
        )
        buttons = []
        if section == "verification":
            choice = group.settings.get("verification_failure_action", "permanent_mute")
            buttons += [
                button(
                    f"{'●' if choice == 'permanent_mute' else '○'} 验证失败：永久禁言（留在群里）",
                    "failure_policy",
                    value="permanent_mute",
                ),
                button(
                    f"{'●' if choice == 'kick' else '○'} 验证失败：踢出群聊（允许重新加入）",
                    "failure_policy",
                    value="kick",
                ),
            ]
        for key in keys:
            value = group.settings[key]
            name = BOOLEANS.get(key) or NUMBERS[key]
            if key in BOOLEANS:
                buttons.append(
                    button(
                        f"{'已开启' if value else '已关闭'} · {name}（点击切换）",
                        "toggle",
                        key=key,
                        value=not value,
                        section=section,
                    )
                )
            else:
                buttons.append(button(f"{name}：{value}", "prompt", kind="setting", key=key, section=section))
        if section == "moderation":
            packs = group.settings.get("emoji_status_blocked_packs", list(DEFAULT_BLOCKED_PACKS))
            buttons.append(
                button(f"状态表情包黑名单：{len(packs)} 个（命中永久禁言）", "emoji_packs")
            )
        buttons.append(button("返回群管理", "group", id=group_id))
        await self.panel(
            actor,
            f"{group.title} · {'验证设置' if section == 'verification' else '防广告设置'}\n"
            "点击开关可直接切换，点击数值后发送新数字。修改立即生效并保留记录。"
            + (
                "\n已入群成员验证失败按所选方式处理；申请入群者尚未入群，失败仅拒绝申请。"
                if section == "verification"
                else "\n发图门槛仅限有可靠入群记录的新成员：发够指定条数的不同纯文字，或入群满 15 天即解除；没有入群记录的成员免除此门槛。广告检测继续生效。"
            ),
            buttons,
            group_id,
            message_id,
            resume={"action": "settings", "data": {"section": section}},
        )

    async def emoji_packs_panel(self, actor, group_id, page=0, message_id=None):
        group = await self.management.authorize(actor, group_id)
        await self.remember(f"bot_prompt:{actor}", None)
        packs = group.settings.get("emoji_status_blocked_packs", list(DEFAULT_BLOCKED_PACKS))
        page = max(0, min(page, max(0, (len(packs) - 1) // 10)))
        visible = packs[page * 10 : page * 10 + 10]
        buttons = [button("新增表情包", "prompt", kind="emoji_packs")]
        buttons += [button(f"移除 {pack}", "emoji_pack_remove", pack=pack, page=page) for pack in visible]
        if page:
            buttons.append(button("上一页", "emoji_packs", page=page - 1))
        if len(packs) > page * 10 + 10:
            buttons.append(button("下一页", "emoji_packs", page=page + 1))
        if packs:
            buttons.append(button("清空状态表情包黑名单", "emoji_packs_clear_confirm"))
        buttons.append(button("返回防广告设置", "settings", section="moderation"))
        await self.panel(
            actor,
            f"{group.title} · 状态表情包黑名单（共 {len(packs)} 个）\n"
            "新增只需发送新链接，原名单自动保留，重复链接不会重复添加。\n"
            + ("当前条目：\n" + "\n".join(visible) if packs else "当前名单为空。")
            + "\n移除或清空名单不会自动解除已有禁言。",
            buttons, group_id, message_id,
            resume={"action": "emoji_packs", "data": {"page": page}},
        )

    async def list_panel(self, actor, group_id, kind, page=0, message_id=None):
        models = {
            "pending": Verification,
            "verification": Verification,
            "rule": Rule,
            "violation": Violation,
            "whitelist": Whitelist,
            "action": Action,
        }
        model = models[kind]
        rows, count = await self.management.records(actor, group_id, model, page, pending=kind == "pending")
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
        await self.panel(actor, "\n".join(lines), buttons, group_id, message_id,
                         resume={"action": "list", "data": {"kind": kind, "page": page}})

    async def detail(self, actor, group_id, kind, identifier, message_id=None):
        await self.management.authorize(actor, group_id)
        models = {
            "pending": Verification,
            "verification": Verification,
            "rule": Rule,
            "violation": Violation,
            "whitelist": Whitelist,
            "action": Action,
        }
        async with self.db.sessions() as session:
            row = await session.get(
                models[kind], (group_id, int(identifier)) if kind == "whitelist" else identifier
            )
        if not row or row.group_id != group_id:
            raise ManagementError(404, "记录不存在，请刷新列表")
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
                }.get(row.failure_action, row.failure_action)
            choices = (
                ("release", "reissue", "ban")
                if row.state in ACTIVE
                else ("release", "reissue")
                if row.state == "failed" and row.failure_action == "permanent_mute"
                else ("unban",)
                if row.state == "failed" and row.failure_action in ("decline", "legacy_ban")
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
            text = f"{KINDS[row.kind]}：{row.value}\n状态：{'启用' if row.enabled else '停用'}\n匹配：{MODES[row.mode]}\n范围：{SCOPES[row.scope]}\n处置：昵称违规删消息并永久禁言；正文违规只删消息"
            if row.scope == "name_and_message":
                text += f"\n正文还需命中：{row.context_value}"
            buttons = [
                button("停用" if row.enabled else "启用", "rule_toggle", id=row.id, enabled=not row.enabled),
                button("修改内容", "prompt", kind="rule", id=row.id, rule_kind=row.kind),
            ]
            if row.kind == "keyword":
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
            text = f"用户 {row.user_id} · {clock(row.created_at)}\n昵称：{row.sender_name}\n{row.excerpt[:600]}\n原因：{'；'.join(row.reasons)}\n处置范围：{'昵称禁言 ' if row.profile_violation else ''}{'删除消息' if row.message_violation else ''}\n已删除的 Telegram 消息无法原地恢复。"
            async with self.db.sessions() as session:
                actions = await violation_actions(session, group_id, row.id)
            for operation in actions:
                text += (
                    f"\n{OPERATIONS.get(operation.kind, operation.kind)}：{STATE.get(operation.state, operation.state)}"
                    + (f"（{operation.error}）" if operation.error else "")
                )
            if not row.sender_chat:
                choices = (
                    ("revoke_warning", "unmute", "unban")
                    if row.warning_active
                    else ("unmute",)
                    if row.profile_violation
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
            text = f"{OPERATIONS.get(row.kind, row.kind)}\n状态：{STATE.get(row.state, row.state)}\n用户：{row.user_id or '—'}\n操作者：{row.actor_id or '系统'}\n原因：{row.reason or '—'}\n{row.error}\n{clock(row.created_at)}（北京时间）"
            result = row.payload.get("result", {})
            if row.kind == "admin":
                text += f"\n目标管理员：{result.get('id', '—')}\n群授权：{result.get('group_ids', [])}\n资格：{'有效' if result.get('active') else '已撤销'}"
            elif row.kind == "whitelist":
                text += f"\n目标成员：{result.get('user_id', '—')}"
            elif row.kind in ("retry", "retry_event"):
                text += f"\n目标记录：{result.get('event_id', result.get('action_id', '—'))}"
        buttons.append(button("返回列表", "list", kind=kind))
        await self.panel(actor, text, buttons, group_id, message_id,
                         resume={"action": "detail", "data": {"kind": kind, "id": identifier}})

    async def prompt(self, actor, group_id, data):
        await self.management.authorize(actor, group_id, write=True)
        group = await self.management.authorize(actor, group_id)
        kind = data["kind"]
        instructions = {
            "emoji_packs": "请发送要新增的 t.me/addemoji 链接或表情包短名称，多个用空格、逗号或换行分隔。只追加新条目，原名单自动保留并去重。移除或清空请返回黑名单面板操作。仅检查名字旁的表情状态，命中永久禁言。"
            + f"当前已有 {len(group.settings.get('emoji_status_blocked_packs', DEFAULT_BLOCKED_PACKS))} 个，可返回面板查看。",
            "archive_query": "请发送要搜索的文字（包含历史编辑版本），发送 - 清除关键词。",
            "archive_sender": "请发送用户或频道的数字 ID，发送 - 清除发送者筛选。",
            "archive_dates": "请发送北京时间范围，例如 2026-10-01 00:00 / 2026-10-20 00:00；发送 - 清除时间筛选。",
            "setting": f"请发送新的 {NUMBERS.get(data.get('key'), '数值')}。",
            "chat_setting": f"请发送新的{CHAT_FIELDS.get(data.get('key'), '聊天设置')}。性格直接用自然语言描述即可。",
            "rule": "请发送一条规则内容。域名只填主机名，例如 example.com；关键词默认包含匹配。",
            "preview": "请发送要测试的消息，不会执行处罚。",
            "whitelist": "请发送 Telegram 用户 ID，可在空格后填写备注。默认免验证、免自动处罚。",
            "white_note": "请发送新的备注。",
            "rule_context": "现有规则内容作为昵称关键词。请发送正文中还需同时命中的关键词。昵称和正文同时命中时永久禁言并删消息。",
            "member": f"{OPERATIONS.get(data.get('operation'), '成员操作')}：请发送 Telegram 用户 ID，可在空格后填写原因。",
        }
        await self.remember(
            f"bot_prompt:{actor}", {"group_id": group_id, "data": data, "expires": now() + 900}
        )
        await self.send(actor, f"{group.title}\n{instructions[kind]}\n发送 /cancel 取消；15 分钟内有效。")

    async def confirmation(self, actor, group_id, data, message_id=None):
        group = await self.management.authorize(actor, group_id, write=True)
        descriptions = {"batch": "批量放行所有正在验证的成员", "white_remove": "移出白名单"}
        operation = descriptions.get(data["kind"], OPERATIONS.get(data.get("operation"), "执行管理操作"))
        duration = ""
        if data.get("operation") == "mute":
            seconds = group.settings["mute_seconds"]
            duration = (
                f"\n本群禁言时长：{seconds // 86400} 天"
                if seconds % 86400 == 0
                else f"\n本群禁言时长：{seconds} 秒"
            )
        elif data.get("operation") == "ban":
            duration = f"\n本群临时封禁时长：{group.settings['failure_ban_seconds']} 秒"
        text = f"请确认 · {group.title}\n操作：{operation}\n用户：{data.get('user_id', data.get('id', '全部待验证成员'))}{duration}\n原因：{data.get('reason', 'Telegram 管理员操作')}"
        await self.panel(
            actor,
            text,
            [button("确认执行", "execute", **data), button("取消并返回", "group", id=group_id)],
            group_id,
            message_id,
        )

    async def submit(self, actor, group_id, chat_id, data, request_id):
        group = await self.management.authorize(actor, group_id, write=True)
        body = ActionInput(
            action=data.get("operation", "release"),
            reason=data.get("reason") or "Telegram 管理员操作",
            request_id=request_id,
        )
        if data["kind"] == "batch":
            result = await self.management.release_pending(actor, group_id, body)
        elif data["kind"] == "verification":
            result = await self.management.verification_action(actor, data["id"], body)
        elif data["kind"] == "violation":
            result = await self.management.violation_action(actor, group_id, data["id"], body)
        elif data["kind"] == "white_remove":
            await self.management.whitelist(
                actor, group_id, WhitelistInput(user_id=int(data["id"])), request_id, remove=True
            )
            await self.send(chat_id, "已移出本群白名单。")
            return
        else:
            result = await self.management.member_action(
                actor, group_id, int(data["user_id"]), body.action, request_id, body.reason
            )
        identifiers = result.get("action_ids", [result["id"]] if "id" in result else [])
        await self.management.track(actor, group_id, chat_id, identifiers, request_id)
        await self.send(
            chat_id,
            f"{group.title}\n已提交 {len(identifiers)} 项操作。Telegram 执行后会在此反馈结果；也可在操作记录查看。",
        )

    async def input(self, actor, text, prompt, request_id):
        group_id, data = prompt["group_id"], prompt["data"]
        kind = data["kind"]
        if kind == "chat_setting":
            key = data["key"]
            if key not in CHAT_FIELDS:
                raise ManagementError(422, "聊天配置项不存在")
            value = text
            if key in ("chat_history", "chat_interval", "chat_sticker_interval", "chat_antifraud_interval"):
                if not text.isdecimal():
                    raise ManagementError(422, "请发送有效的整数")
                value = int(text)
            elif key in ("chat_sticker_packs", "chat_antifraud_packs"):
                import re

                value = [] if text.strip() == "-" else re.split(r"[\s,，]+", text.strip())
            await self.management.setting(actor, group_id, key, value, request_id)
            await self.chat_panel(actor, group_id)
        elif kind.startswith("archive_"):
            filters = dict(data.get("filters", {}))
            if kind == "archive_query":
                filters["query"] = "" if text == "-" else text
            elif kind == "archive_sender":
                try:
                    filters["sender_id"] = None if text == "-" else int(text)
                except ValueError as exc:
                    raise ManagementError(422, "请发送有效的数字 ID，或发送 - 清除") from exc
            elif kind == "archive_dates":
                if text == "-":
                    filters.update(after=0, before=0)
                else:
                    try:
                        start, end = text.split("/", 1)
                        filters.update(
                            after=datetime.strptime(start.strip(), "%Y-%m-%d %H:%M")
                            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                            .timestamp(),
                            before=datetime.strptime(end.strip(), "%Y-%m-%d %H:%M")
                            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                            .timestamp(),
                        )
                    except ValueError as exc:
                        raise ManagementError(
                            422, "时间格式示例：2026-10-01 00:00 / 2026-10-20 00:00"
                        ) from exc
            ArchiveFilter.model_validate(filters)
            await self.remember(f"bot_prompt:{actor}", None)
            await self.archive_panel(actor, group_id, filters)
        elif kind == "emoji_packs":
            import re
            if text.strip() == "-":
                raise ManagementError(422, "这里只新增，不会清空名单。请发送 /cancel 后在黑名单面板点击清空。")
            body = EmojiPackChange(
                packs=[item for item in re.split(r"[\s,，]+", text.strip()) if item], request_id=request_id
            )
            result = await self.management.change_status_packs(actor, group_id, body)
            await self.send(
                actor,
                f"已新增 {len(result['added'])} 个表情包，原名单已保留。"
                if result["added"] else "这些表情包已在黑名单中，无需重复添加。",
            )
            await self.emoji_packs_panel(actor, group_id)
        elif kind == "setting":
            if not text.isdecimal():
                raise ManagementError(422, "请发送有效的非负整数；发送 /cancel 可取消")
            await self.management.setting(actor, group_id, data["key"], int(text), request_id)
            await self.send(actor, "群设置已保存。")
            await self.settings_panel(actor, group_id, data["section"])
        elif kind == "rule":
            values = {"kind": data["rule_kind"], "value": text}
            if data.get("id"):
                async with self.db.sessions() as session:
                    old = await session.get(Rule, data["id"])
                if not old or old.group_id != group_id:
                    raise ManagementError(404, "规则不存在")
                values = {**row_dict(old), "value": text}
            await self.management.rule(actor, group_id, RuleInput(**values), data.get("id", ""), request_id)
            await self.send(actor, "规则已保存。")
            await self.list_panel(actor, group_id, "rule")
        elif kind == "preview":
            name, body = "", text
            if text.startswith("昵称:") or text.startswith("昵称："):
                first, _, body = text.partition("\n")
                name = first[3:]
            result = await self.management.preview(actor, group_id, PreviewInput(text=body, sender_name=name))
            await self.send(
                actor,
                "规则测试结果：\n"
                + ("；".join(result["reasons"]) or "未命中当前规则")
                + f"\n将禁言：{'是' if result['mute_member'] else '否'}；将删消息：{'是' if result['delete_message'] else '否'}",
            )
        elif kind == "rule_context":
            async with self.db.sessions() as session:
                old = await session.get(Rule, data["id"])
            if not old or old.group_id != group_id:
                raise ManagementError(404, "规则不存在")
            await self.management.rule(
                actor,
                group_id,
                RuleInput(**{**row_dict(old), "scope": "name_and_message", "context_value": text}),
                old.id,
                request_id,
            )
            await self.detail(actor, group_id, "rule", old.id)
        elif kind in ("member", "whitelist"):
            parts = text.split(maxsplit=1)
            if not parts or not parts[0].isdecimal() or int(parts[0]) <= 0:
                raise ManagementError(422, "请发送数字形式的 Telegram 用户 ID")
            target, note = int(parts[0]), parts[1] if len(parts) > 1 else ""
            if kind == "whitelist":
                await self.management.whitelist(
                    actor, group_id, WhitelistInput(user_id=target, note=note), request_id
                )
                await self.send(actor, "已加入本群白名单。")
                await self.list_panel(actor, group_id, "whitelist")
            else:
                await self.confirmation(
                    actor,
                    group_id,
                    {"kind": "member", "operation": data["operation"], "user_id": target, "reason": note},
                )
        elif kind == "white_note":
            async with self.db.sessions() as session:
                old = await session.get(Whitelist, (group_id, int(data["id"])))
            if not old:
                raise ManagementError(404, "白名单记录不存在")
            await self.management.whitelist(
                actor, group_id, WhitelistInput(**{**row_dict(old), "note": text}), request_id
            )
            await self.detail(actor, group_id, "whitelist", data["id"])
        await self.remember(f"bot_prompt:{actor}", None)

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

    async def system(self, actor, message_id=None):
        await self.management.principal(actor, root=True)
        async with self.db.sessions() as session:
            pending = await session.scalar(
                select(func.count()).select_from(Action).where(Action.state == "pending")
            )
            failed = await session.scalar(
                select(func.count()).select_from(Action).where(Action.state == "failed")
            )
            backup = await session.get(Meta, "last_backup")
            archive_status = await session.get(Meta, "archive:paused")
        from purrai.chat_budget import ChatBudget

        budget = await ChatBudget(self.db, self.management.settings).status(None)
        await self.panel(
            actor,
            f"PurrAI 系统状态\nTelegram：{'已连接' if self.tg.connected else '未连接'}\n待执行任务：{pending}\n失败任务：{failed}\n最近备份：{backup.value if backup else '尚未生成'}\n"
            f"今日模型请求：{budget['global_calls']}/{budget['global_call_limit']}；Token 预算：{budget['global_tokens']}/{budget['global_token_limit']}\n"
            + (f"归档：{archive_status.value}\n" if archive_status else "")
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
        await self.management.principal(actor, root=True)
        model = Inbox if event else Action
        async with self.db.sessions() as session:
            rows = (
                await session.scalars(
                    select(model).where(model.state == "failed").order_by(model.id).offset(page * 5).limit(6)
                )
            ).all()
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
        await self.panel(actor, text, buttons, message_id=message_id,
                         resume={"action": "failures", "data": {"event": event, "page": page}})

    async def route(self, actor, token, record, message_id):
        action, group_id, data = record["action"], record["group_id"], record["data"]
        request_id = f"tg-button:{token}"
        await self.management.principal(actor)
        if group_id is not None:
            await self.management.authorize(actor, group_id)
        if action not in ("prompt", "admin_add"):
            await self.remember(f"bot_prompt:{actor}", None)
        if action == "access_panel":
            await self.access_panel(actor, data["id"], message_id)
        elif action == "group_access":
            await self.management.group_access(actor, data["id"], data["approved"])
            await self.choose_groups(actor, message_id=message_id)
        elif action == "groups":
            await self.remember(f"bot_prompt:{actor}", None)
            await self.choose_groups(actor, data.get("page", 0), message_id)
        elif action == "group":
            await self.group_panel(actor, data["id"], message_id)
        elif action in ("activate", "permissions"):
            result = await self.management.permissions(actor, group_id, activate=action == "activate")
            await self.send(
                actor, "机器人权限完整。" if result["ok"] else "缺少权限：" + "、".join(result["missing"])
            )
            await self.group_panel(actor, group_id, message_id)
        elif action == "pause":
            await self.management.settings_patch(actor, group_id, SettingsPatch(enabled=False), request_id)
            await self.group_panel(actor, group_id, message_id)
        elif action == "join_link":
            result = await self.management.join_link(actor, group_id, request_id)
            await self.management.track(actor, group_id, actor, [result["id"]], request_id)
            await self.send(actor, "已受理，生成后会在此返回申请入群链接。请替换原有直接入群入口。")
        elif action == "settings":
            await self.settings_panel(actor, group_id, data["section"], message_id)
        elif action == "chat_toggle":
            if data["key"] not in ("chat_enabled", "chat_proactive", "chat_stickers", "chat_antifraud"):
                raise ManagementError(422, "聊天配置项不存在")
            await self.management.setting(actor, group_id, data["key"], data["value"], request_id)
            await self.chat_panel(actor, group_id, message_id)
        elif action == "chat_reset":
            await self.management.reset_chat(actor, group_id, request_id)
            await self.send(actor, "已重新开始聊天；历史归档仍然保留。")
            await self.chat_panel(actor, group_id, message_id)
        elif action == "failure_policy":
            await self.management.setting(
                actor, group_id, "verification_failure_action", data["value"], request_id
            )
            await self.settings_panel(actor, group_id, "verification", message_id)
        elif action == "archive":
            await self.archive_panel(actor, group_id, data.get("filters"), data.get("page", 0), message_id)
        elif action == "archive_detail":
            await self.archive_detail(actor, group_id, data, message_id)
        elif action == "archive_toggle":
            group = await self.management.authorize(actor, group_id)
            await self.management.setting(
                actor,
                group_id,
                "archive_enabled",
                not group.settings.get("archive_enabled", True),
                request_id,
            )
            await self.archive_panel(actor, group_id, message_id=message_id)
        elif action == "archive_export":
            task = await self.management.archive_export(
                actor, group_id, ArchiveFilter.model_validate(data["filters"]), request_id
            )
            await self.management.track(actor, group_id, actor, [task["id"]], request_id)
            await self.send(actor, "导出已受理，完成后会在私聊发送 JSONL 文件；可在操作记录查看结果。")
        elif action == "toggle":
            if data["key"] not in BOOLEANS:
                raise ManagementError(422, "配置项不存在")
            await self.management.setting(actor, group_id, data["key"], data["value"], request_id)
            await self.settings_panel(actor, group_id, data["section"], message_id)
        elif action == "list":
            await self.list_panel(actor, group_id, data["kind"], data.get("page", 0), message_id)
        elif action == "detail":
            await self.detail(actor, group_id, data["kind"], data["id"], message_id)
        elif action == "members":
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
        elif action == "emoji_packs":
            await self.emoji_packs_panel(actor, group_id, data.get("page", 0), message_id)
        elif action == "emoji_pack_remove":
            await self.management.change_status_packs(
                actor, group_id,
                EmojiPackChange(operation="remove", packs=[data["pack"]], request_id=request_id),
            )
            await self.send(actor, f"已从黑名单移除 {data['pack']}；已有禁言不会自动解除。")
            await self.emoji_packs_panel(actor, group_id, data.get("page", 0), message_id)
        elif action == "emoji_packs_clear_confirm":
            await self.management.authorize(actor, group_id, write=True)
            await self.panel(
                actor, "确认清空本群的状态表情包黑名单？已有禁言不会自动解除。",
                [button("确认清空", "emoji_packs_clear"), button("取消并返回", "emoji_packs")],
                group_id, message_id,
            )
        elif action == "emoji_packs_clear":
            await self.management.change_status_packs(
                actor, group_id, EmojiPackChange(operation="clear", request_id=request_id),
            )
            await self.emoji_packs_panel(actor, group_id, message_id=message_id)
        elif action == "prompt":
            await self.prompt(actor, group_id, data)
        elif action == "confirm":
            await self.confirmation(actor, group_id, data, message_id)
        elif action == "execute":
            await self.submit(actor, group_id, actor, data, request_id)
        elif action in ("rule_toggle", "rule_mode", "rule_scope"):
            async with self.db.sessions() as session:
                row = await session.get(Rule, data["id"])
            if not row or row.group_id != group_id:
                raise ManagementError(404, "规则不存在")
            key = {"rule_toggle": "enabled", "rule_mode": "mode", "rule_scope": "scope"}[action]
            values = {**row_dict(row), key: data[key]}
            await self.management.rule(actor, group_id, RuleInput(**values), row.id, request_id)
            await self.detail(actor, group_id, "rule", row.id, message_id)
        elif action == "white_toggle":
            async with self.db.sessions() as session:
                row = await session.get(Whitelist, (group_id, int(data["id"])))
            if not row or data["key"] not in ("verification", "moderation"):
                raise ManagementError(404, "白名单记录不存在")
            await self.management.whitelist(
                actor, group_id, WhitelistInput(**{**row_dict(row), data["key"]: data["value"]}), request_id
            )
            await self.detail(actor, group_id, "whitelist", row.user_id, message_id)
        elif action == "admins":
            await self.admins(actor, message_id, data.get("page", 0))
        elif action in ("admin_add", "admin_detail", "admin_grant", "admin_active"):
            await self.send(actor, "已改为自动识别群管理员，请在 Telegram 群设置中管理身份。")
            await self.admins(actor, message_id)
        elif action == "system":
            await self.system(actor, message_id)
        elif action == "failures":
            await self.failures(actor, data.get("event", False), data.get("page", 0), message_id)
        elif action == "retry":
            event = data.get("event", False)
            await self.management.retry(actor, data["id"], event, request_id)
            if not event:
                async with self.db.sessions() as session:
                    row = await session.get(Action, data["id"])
                if row.group_id:
                    await self.management.track(actor, row.group_id, actor, [row.id], request_id)
            await self.send(actor, "重试已受理。可返回系统状态查看实际处理结果。")
            await self.failures(actor, event, message_id=message_id)
        elif action == "backup":
            result = await self.management.backup(actor, request_id)
            await self.send(actor, f"一致性备份已保存：{result['file']}")
        else:
            if group_id is not None:
                await self.group_panel(actor, group_id, message_id)
            else:
                await self.choose_groups(actor, message_id=message_id)

    @staticmethod
    def legacy_resume(record):
        action, group_id, data = record["action"], record["group_id"], record["data"]
        if action in ("chat_toggle", "chat_reset"):
            return {"action": "settings", "data": {"section": "chat"}}
        if action in ("toggle", "failure_policy"):
            return {"action": "settings", "data": {"section": data.get("section", "verification")}}
        if action in ("archive_toggle", "archive_export"):
            return {"action": "archive", "data": {"filters": data.get("filters", {})}}
        if action in ("rule_toggle", "rule_mode", "rule_scope", "white_toggle"):
            return {"action": "detail", "data": {"kind": "whitelist" if action == "white_toggle" else "rule",
                                                  "id": data["id"]}}
        if action == "emoji_pack_remove":
            return {"action": "emoji_packs", "data": {"page": data.get("page", 0)}}
        if action == "group_access":
            return {"action": "access_panel", "data": {"id": data["id"]}}
        if action == "retry":
            return {"action": "failures", "data": {"event": data.get("event", False)}}
        if action == "backup":
            return {"action": "system", "data": {}}
        return ({"action": "group", "data": {"id": group_id}} if group_id is not None
                else {"action": "groups", "data": {}})

    async def resume_panel(self, actor, token, record, message_id):
        if record["action"] == "execute":
            return await self.confirmation(actor, record["group_id"], record["data"], message_id)
        if record["action"] == "emoji_packs_clear":
            resume = {"action": "emoji_packs_clear_confirm", "data": {}}
        else:
            resume = record.get("resume") or self.legacy_resume(record)
        await self.route(actor, token, {**record, **resume}, message_id)

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
                await self.management.service.acknowledge(query["id"], "正在恢复管理面板")
                await self.route(actor, token, {**resume, "group_id": group_id}, message.get("message_id"))
                return True
            if record["actor"] != actor:
                raise ManagementError(403, "此按钮属于其他管理员，请使用你自己的管理面板")
            expires = record.get("expires")
            if record["action"] not in NAVIGATION_ACTIONS and expires is not None and expires <= now():
                await self.management.service.acknowledge(query["id"], "已接续面板，请核对后再次选择要执行的操作")
                await self.resume_panel(actor, token, record, message.get("message_id"))
                return True
            await self.management.service.acknowledge(query["id"], "正在处理")
            await self.route(actor, token, record, message.get("message_id"))
        except (ManagementError, ValidationError) as exc:
            await self.management.service.acknowledge(query["id"], "操作未完成")
            await self.send(actor, self.explain(exc))
        return True

    @staticmethod
    def explain(error):
        if isinstance(error, ValidationError):
            names = {
                **BOOLEANS,
                **NUMBERS,
                "value": "规则内容",
                "note": "备注",
                "reason": "原因",
                "user_id": "用户 ID",
                "id": "管理员 ID",
                "name": "管理员备注",
            }
            details = []
            for item in error.errors():
                field = str(item["loc"][-1]) if item["loc"] else "输入"
                name, context = names.get(field, "输入"), item.get("ctx", {})
                if item["type"] == "greater_than_equal":
                    message = f"不能小于 {context['ge']}"
                elif item["type"] == "less_than_equal":
                    message = f"不能大于 {context['le']}"
                elif item["type"] == "greater_than":
                    message = f"必须大于 {context['gt']}"
                elif item["type"] == "string_too_long":
                    message = f"不能超过 {context['max_length']} 个字符"
                elif item["type"] == "string_too_short":
                    message = "不能为空"
                else:
                    message = str(context.get("error", "格式不正确，请检查后重试"))
                details.append(f"{name}：{message}")
            return "输入不符合要求：" + "；".join(details)
        return str(error)

    async def message(self, message):
        text = (message.get("text") or "").strip()
        actor = message.get("from", {}).get("id")
        if not actor:
            return False
        private = message["chat"]["type"] == "private"
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text.startswith("/") else ""
        parts = text.split(maxsplit=1)
        tail = parts[1] if len(parts) > 1 else ""
        commands = {
            "/start",
            "/id",
            "/help",
            "/admin",
            "/groups",
            "/status",
            "/cancel",
            "/mute",
            "/ban",
            "/unmute",
            "/unban",
            "/release",
            "/reissue",
        }
        if not private and command not in commands:
            return False
        addressed = text.split(maxsplit=1)[0] if text else ""
        if (
            "@" in addressed
            and addressed.split("@", 1)[1].lower() != getattr(self.tg, "username", "").lower()
        ):
            return False
        chat_id = message["chat"]["id"]
        request_id = f"tg-message:{chat_id}:{message['message_id']}"
        try:
            if command == "/id":
                if message.get("sender_chat"):
                    await self.send(chat_id, "请使用个人账号私聊机器人发送 /id，匿名身份无法识别真实账号。")
                else:
                    username = message.get("from", {}).get("username")
                    await self.send(
                        chat_id,
                        f"你的 Telegram 数字 ID：{actor}"
                        + (f"\n用户名：@{username}" if username else "")
                        + "\n配置超级管理员时填写上面的数字，不要填写 @用户名。",
                    )
                return True
            if command in ("/help", "/start"):
                if private:
                    try:
                        await self.management.principal(actor)
                    except ManagementError:
                        await self.send(
                            chat_id,
                            "入群验证请在验证码图片下方选择与图片数字相同的完整答案，点击一次即可。群管理员可发送 /admin；查看自己的数字 ID 请发送 /id。",
                        )
                        return True
                    await self.choose_groups(actor)
                else:
                    await self.send(chat_id, "新成员请在验证码图片下方作答；管理员请私聊机器人发送 /admin。")
                return True
            await self.management.principal(actor)
            if not private and message.get("sender_chat"):
                raise ManagementError(403, "匿名管理员请使用个人账号私聊机器人操作")
            if command in ("/admin", "/groups"):
                if private:
                    await self.remember(f"bot_prompt:{actor}", None)
                    await self.choose_groups(actor)
                else:
                    await self.management.authorize(actor, chat_id)
                    username = getattr(self.tg, "username", "")
                    await self.send(
                        chat_id,
                        "请私聊机器人发送 /admin"
                        + (f"：https://t.me/{username}?start=admin" if username else "。"),
                    )
                return True
            if command == "/cancel":
                await self.remember(f"bot_prompt:{actor}", None)
                await self.send(chat_id, "已取消当前输入。私聊发送 /admin 返回管理面板。")
                return True
            group_id = await self.recall(f"bot_selected:{actor}") if private else chat_id
            if command in ("/mute", "/ban", "/unmute", "/unban", "/release", "/reissue"):
                if group_id is None:
                    await self.choose_groups(actor)
                    await self.send(chat_id, "请先选择群，再发送成员操作命令。")
                    return True
                reply = message.get("reply_to_message") or {}
                if not private and reply.get("from") and not reply.get("sender_chat"):
                    target, reason = reply["from"]["id"], tail
                else:
                    parts = tail.split(maxsplit=1)
                    if not parts or not parts[0].isdecimal():
                        raise ManagementError(
                            422, f"用法：{command} 用户数字ID 原因。群内也可以回复成员消息后发送命令。"
                        )
                    target, reason = int(parts[0]), parts[1] if len(parts) > 1 else ""
                await self.submit(
                    actor,
                    group_id,
                    chat_id,
                    {"kind": "member", "user_id": target, "operation": command[1:], "reason": reason},
                    request_id,
                )
                return True
            if command == "/status":
                if group_id is None:
                    await self.choose_groups(actor)
                else:
                    group = await self.management.authorize(actor, group_id)
                    await self.send(
                        chat_id,
                        f"PurrAI · {group.title}\n群保护：{STATE.get(group.status, group.status)}\n"
                        f"图片验证：{'开启' if group.settings['verification_enabled'] else '关闭'}\n"
                        f"Telegram：{'已连接' if self.tg.connected else '未连接'}",
                    )
                return True
            if private:
                prompt = await self.recall(f"bot_prompt:{actor}")
                if prompt and prompt["expires"] > now() and not command:
                    if prompt["data"]["kind"] == "admin":
                        await self.remember(f"bot_prompt:{actor}", None)
                        await self.admins(actor)
                    else:
                        await self.input(actor, text, prompt, request_id)
                else:
                    await self.send(chat_id, "请发送 /admin 打开管理面板，或 /cancel 取消输入。")
                return True
        except (ManagementError, ValidationError) as exc:
            await self.send(chat_id, self.explain(exc))
            return True
        return False
