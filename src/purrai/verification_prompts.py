"""Configurable verification prompts and channel access checks."""
import re
import secrets
from urllib.parse import urlsplit

from purrai import captcha


def channel_username(value):
    value = value.strip().rstrip("。")
    if not value:
        return ""
    if value.startswith(("https://", "http://")):
        parsed = urlsplit(value)
        if parsed.hostname not in ("t.me", "telegram.me") or parsed.query or parsed.fragment:
            raise ValueError("请填写公开频道的 @用户名或 t.me 链接")
        value = parsed.path.strip("/")
    value = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        raise ValueError("请填写公开频道的 @用户名或 t.me 链接")
    return "@" + value


async def resolve_channel(call, target, bot_id):
    if not target:
        raise ValueError("请先配置订阅验证使用的频道")
    chat = await call("get_chat", chat_id=target)
    if chat.get("type") != "channel" or not chat.get("username"):
        raise ValueError("订阅验证需要公开频道，请检查频道链接")
    await channel_rights(call, chat["id"], bot_id)
    return {"channel_id": chat["id"], "channel_title": chat.get("title", "频道"),
            "channel_url": "https://t.me/" + chat["username"]}


async def channel_rights(call, channel_id, bot_id):
    member = await call("get_chat_member", chat_id=channel_id, user_id=bot_id)
    if member.get("status") not in ("administrator", "creator"):
        raise ValueError("机器人需要成为目标频道管理员，才能可靠检查订阅；未因此处罚用户")


def text_question():
    left, right = secrets.randbelow(15) + 1, secrets.randbelow(10) + 1
    if secrets.randbelow(2):
        left, right = max(left, right), min(left, right)
        answer, question = left - right, f"{left} − {right} = ？"
    else:
        answer, question = left + right, f"{left} + {right} = ？"
    choices = {str(answer)}
    while len(choices) < 4:
        choices.add(str(secrets.randbelow(36)))
    choices = list(choices)
    secrets.SystemRandom().shuffle(choices)
    return question, str(answer), choices


def keyboard(verification):
    if verification.method != "channel":
        return captcha.keyboard(verification.id, verification.nonce, verification.choices)
    return {"inline_keyboard": [
        [{"text": "订阅频道", "url": verification.challenge["channel_url"]}],
    ]}


def text_prompt(verification, group):
    prefix = f"{verification.user_name[:60]}，欢迎加入！\n"
    if verification.method == "channel":
        title = verification.challenge.get("channel_title", "指定频道")
        text = f"请订阅「{title}」。订阅后会自动放行，无需返回点击确认。\n"
    else:
        text = f"请计算：{verification.challenge['question']}\n点击下方正确答案，无需发送消息。\n"
        text += f"剩余作答次数：{group.settings['max_attempts'] - verification.attempts}\n"
    text += f"验证消息送达后 {group.settings['verification_timeout']} 秒内完成。\n"
    if verification.method != "channel":
        text += "只有你本人可以作答。\n"
    failed = "超时" if verification.method == "channel" else "超时或答错次数用尽"
    if verification.flow == "join_request":
        text += f"通过后批准入群；{failed}会拒绝本次申请。"
    elif group.settings.get("verification_failure_action", "permanent_mute") == "permanent_mute":
        text += f"通过后恢复发言；{failed}将永久禁言，可联系管理员放行。"
    else:
        text += f"通过后恢复发言；{failed}将移出群聊，可重新加入验证。"
    return prefix + text
