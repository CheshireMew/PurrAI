from types import SimpleNamespace

import pytest
from sqlalchemy import select

from conftest import message
from purrai.chat_generation import ChatGeneration
from purrai.models import ArchivedMessage, ChatMessage
from purrai.schemas import GroupSettings
from purrai.telegram import TelegramGateway
from test_chat import configured, due


@pytest.mark.parametrize("first,last,expected", [
    ("AI柴郡喵", None, "AI柴郡喵"),
    ("另一只", "机器人", "另一只 机器人"),
])
async def test_initialization_reads_display_name_and_refreshes_on_reconnect(monkeypatch, first, last, expected):
    gateway = TelegramGateway("")
    profile = {"id": 999, "username": "another_bot", "first_name": first}
    if last is not None:
        profile["last_name"] = last
    calls = []

    async def call(method, **kwargs):
        calls.append(method)
        return profile

    monkeypatch.setattr(gateway, "call", call)
    await gateway.initialize()
    assert gateway.display_name == expected and gateway.username == "another_bot" and gateway.connected
    profile = {"id": 999, "username": "another_bot", "first_name": "改名后的机器人"}
    await gateway.initialize()
    assert gateway.display_name == "改名后的机器人"
    assert calls == ["get_me", "get_me"]


@pytest.mark.parametrize("name", ["AI柴郡喵", "小橘", "Another Bot"])
def test_prompt_uses_actual_bot_name_without_brand_prefix(name):
    cfg = GroupSettings(chat_name="群里叫法", chat_persona="安静、好奇。")
    telegram = SimpleNamespace(display_name=name)
    prompt = ChatGeneration(None, None, telegram).prompt(cfg)
    assert prompt.startswith(f"你正在扮演一位生活在群里的女仆，你的名字是{name}。安静、好奇。")
    assert "群里叫法" not in prompt


def test_prompt_falls_back_to_configured_name_before_telegram_initialization():
    prompt = ChatGeneration(None, None, TelegramGateway("")).prompt(GroupSettings(chat_name="离线昵称"))
    assert "你的名字是离线昵称。" in prompt


async def test_prompt_history_and_archive_share_the_real_bot_identity(env):
    chat, model = await configured(env, chat_name="群里叫法")
    _, db, telegram, _ = env
    telegram.display_name = "完全不同的机器人"
    await chat.observe(message(1, "@purrai_bot 你叫什么？"))
    await due(chat)
    assert await chat.run_one()
    assert "你的名字是完全不同的机器人。" in model.calls[0][0]
    async with db.sessions() as session:
        stored = await session.scalar(select(ChatMessage).where(ChatMessage.is_bot.is_(True)))
        archived = await session.scalar(select(ArchivedMessage).where(ArchivedMessage.sender_type == "bot"))
        assert stored.sender_name == archived.sender_name == telegram.display_name
    await chat.observe(message(2, "@purrai_bot 再聊一句"))
    await due(chat)
    assert await chat.run_one()
    previous = [row for row in model.calls[-1][1]["messages"] if row["bot"]]
    assert previous and all(row["name"] == telegram.display_name for row in previous)
