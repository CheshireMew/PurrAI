"""Telegram panels for configuration panels."""

from datetime import datetime
from zoneinfo import ZoneInfo

from purrai.admin_labels import BOOLEANS, CHAT_BUTTON_LABELS, CHAT_FIELDS, NUMBERS
from purrai.bot_common import button
from purrai.models import now
from purrai.pack_names import DEFAULT_BLOCKED_PACKS
from purrai.schemas import (
    GroupSettings,
)


class ConfigurationPanels:
    def __init__(self, management, ui, state):
        self.management, self.panel, self.send = management, ui.panel, ui.send
        self.tg, self.remember = ui.tg, state.remember

    async def chat_panel(self, actor, group_id, message_id=None):
        group = await self.management.authorize(actor, group_id)
        cfg = GroupSettings.model_validate(group.settings)
        status = await self.management.chat_status(actor, group_id)
        text = (
            f"{group.title} · 群聊天设置\n"
            f"模型：{status['model'] if status['configured'] else '尚未配置；请在服务器填写聊天 API 地址、Key 和模型名'}\n"
            f"昵称：{cfg.chat_name}\n性格：{cfg.chat_persona or '自然随和'}\n"
            f"最近上下文：{cfg.chat_history} 条，同群不同话题分开\n"
            f"聊天忽略口令：{'、'.join(cfg.chat_ignored_texts)[:300] or '未设置'}（完整匹配）\n"
            f"自动识别口令：{'开启' if cfg.chat_command_learning else '关闭'}；7 天内 {cfg.chat_command_threshold} 次、至少 3 位群友\n"
            "带数字的积分红包及发给其他机器人的结构化指令直接排除。\n"
            "不再固定等待，直接处理明确提问；模型生成及接口限流仍需处理时间。\n"
            "普通含图消息不主动接话；被 @、叫昵称或回复时仍回答，说明看不到图片并依据文字。\n"
            f"主动接话最多每 {cfg.chat_interval} 秒检查一次；模型觉得不适合时会保持安静。\n"
            f"聊天贴纸：{len(cfg.chat_sticker_packs)} 包，至少间隔 {cfg.chat_sticker_interval} 秒\n"
            f"防诈提醒：至少间隔 {cfg.chat_antifraud_interval // 3600} 小时，白天且群内有新讨论时才发\n"
            f"贴纸列表：已加载 {status.get('stickers', {}).get('ready', 0)}/{status.get('stickers', {}).get('total', 0)} 包\n"
            f"聊天 emoji 标签：{status.get('stickers', {}).get('emoji_count', 0)} 种，自动包含各包全部标签\n"
            "@机器人、叫昵称或回复它都可以开始聊；最近正在聊的群友可以自然接着说。\n"
            f"待处理：{status['pending']}\n" + (f"最近问题：{status['error']}\n" if status["error"] else "")
        )
        commands = status.get("commands", {}).get("items", [])
        if commands:
            text += "口令计数：" + "；".join(
                f"{item['text']} {item['count']}次/{item['users']}人{'（已忽略）' if item['ignored'] else ''}"
                for item in commands[:8]
            ) + "\n"
        budget = status.get("budget", {})
        refreshed = datetime.fromtimestamp(now(), ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        text += (
            f"统计刷新：{refreshed}（北京时间；点击刷新才更新）\n"
            f"本群今日累计请求：已用 {budget.get('group_calls', 0)} 次 / 上限 {budget.get('group_call_limit', '—')} 次\n"
            f"本群今日 Token 额度占用：{budget.get('group_tokens', 0):,} / {budget.get('group_token_limit', 0):,}\n"
            "统计包含全群所有成员和话题，保持安静和重试也计数。\n"
            "Token 含历史上下文及请求预留，按实际用量结算后可能回落；不是单条消息用量。\n"
            "每日额度由所有者设置，北京时间零点重置；验证和防广告不占此额度。"
        )
        buttons = [
            button(
                f"{'已开启' if cfg.chat_command_learning else '已关闭'} · 自动识别口令",
                "chat_toggle", key="chat_command_learning", value=not cfg.chat_command_learning,
            ),
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
            button(
                f"{'已开启' if cfg.chat_stickers else '已关闭'} · 聊天使用贴纸",
                "chat_toggle",
                key="chat_stickers",
                value=not cfg.chat_stickers,
            ),
            button(
                f"{'已开启' if cfg.chat_antifraud else '已关闭'} · 偶尔发防诈贴纸",
                "chat_toggle",
                key="chat_antifraud",
                value=not cfg.chat_antifraud,
            ),
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
        await self.panel(
            actor,
            text,
            buttons,
            group_id,
            message_id,
            resume={"action": "settings", "data": {"section": "chat"}},
        )

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
            buttons.append(button(f"状态表情包黑名单：{len(packs)} 个（发言永久禁言）", "emoji_packs"))
            buttons.append(button(f"允许的 Telegram 链接：{len(group.settings.get('telegram_allowed_links', []))} 个", "prompt", kind="telegram_links"))
        buttons.append(button("返回群管理", "group", id=group_id))
        await self.panel(
            actor,
            f"{group.title} · {'验证设置' if section == 'verification' else '防广告设置'}\n"
            "点击开关可直接切换，点击数值后发送新数字。修改立即生效并保留记录。"
            + (
                "\n已入群成员验证失败按所选方式处理；申请入群者尚未入群，失败仅拒绝申请。"
                if section == "verification"
                else "\n普通成员分享联系人会删除消息并永久封禁，不限入群时长。\n明确广告招揽、确认的外群链接和外部私密邀请不限入群时长，删消息并永久封禁；本群已知链接和允许名单豁免链接检查。明确举报或防骗引用只删除外群链接。\n观察期默认 3 天且检查前 3 条消息，限制链接、@账号或群组、用户及频道转发；命中删除消息。钱包及代币合约地址拦截对所有普通成员生效，不限入群时间，命中只删消息。\n发图门槛仅限有可靠入群记录的新成员：发够指定条数的不同纯文字，或入群满 15 天即解除；没有入群记录的成员免除此门槛。广告检测继续生效。"
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
            + "\n入群命中永久封禁；已在群内发言命中则删消息并永久禁言；移除或清空名单不会自动解除已有封禁或禁言。",
            buttons,
            group_id,
            message_id,
            resume={"action": "emoji_packs", "data": {"page": page}},
        )
