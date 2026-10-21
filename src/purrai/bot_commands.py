"""Telegram slash commands and text event ingress."""

from pydantic import ValidationError

from purrai.admin_labels import STATE
from purrai.bot_common import explain
from purrai.errors import ManagementError
from purrai.models import now


class BotCommands:
    def __init__(self, management, ui, state, panels, inputs):
        self.management, self.panels, self.inputs = management, panels, inputs
        self.tg, self.send = ui.tg, ui.send
        self.remember, self.recall = state.remember, state.recall

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
                        await self.panels.choose_groups(actor)
                    except ManagementError:
                        await self.send(
                            chat_id,
                            "入群验证请在验证码图片下方选择与图片数字相同的完整答案，点击一次即可。群管理员可发送 /admin；查看自己的数字 ID 请发送 /id。",
                        )
                        return True
                else:
                    await self.send(chat_id, "新成员请在验证码图片下方作答；管理员请私聊机器人发送 /admin。")
                return True
            if not private and message.get("sender_chat"):
                raise ManagementError(403, "匿名管理员请使用个人账号私聊机器人操作")
            if command in ("/admin", "/groups"):
                if private:
                    await self.remember(f"bot_prompt:{actor}", None)
                    await self.panels.choose_groups(actor)
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
                    await self.panels.choose_groups(actor)
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
                await self.inputs.submit(
                    actor,
                    group_id,
                    chat_id,
                    {"kind": "member", "user_id": target, "operation": command[1:], "reason": reason},
                    request_id,
                )
                return True
            if command == "/status":
                if group_id is None:
                    await self.panels.choose_groups(actor)
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
                        await self.panels.admins(actor)
                    else:
                        await self.inputs.input(actor, text, prompt, request_id)
                else:
                    await self.send(chat_id, "请发送 /admin 打开管理面板，或 /cancel 取消输入。")
                return True
        except (ManagementError, ValidationError) as exc:
            await self.send(chat_id, explain(exc))
            return True
        return False
