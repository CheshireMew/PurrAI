from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BufferedInputFile
from pydantic import BaseModel


def telegram_data(value):
    """Keep API field names and only fields actually received from Telegram.

    SDK defaults inside link previews are Default sentinels, not JSON values.
    Excluding unset fields preserves explicit False/zero values without injecting
    client-side defaults into the durable inbox or the message archive.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True, exclude_unset=True, by_alias=True)
    if isinstance(value, list):
        return [telegram_data(item) for item in value]
    return value


class TelegramGateway:
    def __init__(self, token: str, proxy: str = ""):
        self.bot = Bot(token, session=AiohttpSession(proxy=proxy or None)) if token else None
        self.user_id = 0
        self.username = ""
        self.display_name = ""
        self.connected = False

    async def call(self, method: str, **kwargs):
        if self.bot is None:
            raise ConnectionError("尚未配置 Telegram Bot Token")
        if method == "send_photo" and isinstance(kwargs.get("photo"), bytes):
            kwargs["photo"] = BufferedInputFile(kwargs["photo"], "verification.png")
        if method == "send_document" and isinstance(kwargs.get("document"), bytes):
            kwargs["document"] = BufferedInputFile(
                kwargs["document"], kwargs.pop("filename", "messages.jsonl")
            )
        kwargs.setdefault("request_timeout", 35 if method == "get_updates" else 10)
        result = await getattr(self.bot, method)(**kwargs)
        return telegram_data(result)

    async def initialize(self):
        me = await self.call("get_me")
        self.user_id = me["id"]
        self.username = me.get("username", "")
        self.display_name = " ".join(filter(None, [me.get("first_name"), me.get("last_name")])).strip()
        self.connected = True

    async def register_commands(self):
        common = [("start", "开始使用"), ("help", "使用帮助"), ("id", "查看我的 Telegram 数字 ID")]
        admin = [("admin", "打开管理面板"), ("groups", "选择管理群"), ("status", "查看当前群状态")]
        moderation = [
            ("release", "人工放行验证"),
            ("reissue", "重新发送验证码"),
            ("mute", "临时禁言"),
            ("ban", "临时封禁"),
            ("unmute", "解除 PurrAI 禁言"),
            ("unban", "解除 PurrAI 封禁"),
        ]
        for scope, commands in (
            ("default", common),
            ("all_private_chats", common + admin + moderation + [("cancel", "取消当前输入")]),
            ("all_chat_administrators", admin + moderation + [("help", "使用帮助")]),
        ):
            await self.call(
                "set_my_commands",
                scope={"type": scope},
                commands=[
                    {"command": command, "description": description} for command, description in commands
                ],
            )

    async def close(self):
        if self.bot:
            await self.bot.session.close()


def bot_display_name(telegram, fallback: str) -> str:
    return getattr(telegram, "display_name", "") or fallback


def is_present(member: dict) -> bool:
    return member.get("status") in ("member", "administrator", "creator") or (
        member.get("status") == "restricted" and member.get("is_member", False)
    )


def is_admin(member: dict) -> bool:
    return member.get("status") in ("administrator", "creator")


SEND_PERMISSIONS = (
    "can_send_messages",
    "can_send_audios",
    "can_send_documents",
    "can_send_photos",
    "can_send_videos",
    "can_send_video_notes",
    "can_send_voice_notes",
    "can_send_polls",
    "can_send_other_messages",
    "can_add_web_page_previews",
    "can_react_to_messages",
)
ALL_PERMISSIONS = SEND_PERMISSIONS + (
    "can_edit_tag",
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
    "can_manage_topics",
)


def muted_permissions(previous: dict | None = None) -> dict:
    return {
        **dict.fromkeys(ALL_PERMISSIONS, False),
        **{
            key: bool((previous or {}).get(key, False))
            for key in ALL_PERMISSIONS
            if key not in SEND_PERMISSIONS
        },
    }


def permission_snapshot(member: dict) -> dict:
    return {key: bool(member.get(key, False)) for key in ALL_PERMISSIONS}
