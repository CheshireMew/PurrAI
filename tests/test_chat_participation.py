import pytest
from sqlalchemy import select

from conftest import message
from purrai.chat import GroupChat
from purrai.chat_model import ChatReply
from purrai.models import Action, ChatTurn, Group, Violation
from test_chat import FakeModel, age_reply, configured
from test_bot_admin import click, markup, open_group


@pytest.mark.parametrize("text", ["在线", "你好啊", "在吗？", "666666666", "１２３４５６", "1,234.56", "@purrai_bot 在线", "柴郡喵，在吗？"])
async def test_low_information_opening_is_silent_without_penalty(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, text))
    assert not await chat.run_one() and not model.calls
    assert not any(method in ("send_message", "send_sticker", "restrict_chat_member", "delete_message") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        assert await session.scalar(select(Action)) is None
        assert await session.scalar(select(Violation)) is None


@pytest.mark.parametrize("text", ["好", "100", "谢谢"])
async def test_short_answer_to_a_confirmed_bot_question_keeps_its_context(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，晚饭预算怎么安排？"))
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        identifier = await session.scalar(select(ChatTurn.sent_message_id))
    await age_reply(chat)
    await chat.observe(message(2, text, reply_to_message={"message_id": identifier, "from": {"id": 999, "is_bot": True}}))
    assert await chat.run_one() and len(model.calls) == 2


async def test_identical_member_message_stays_silent_across_restart_but_other_members_can_talk(env):
    chat, _ = await configured(env)
    text = "柴郡喵，周末去哪走走比较好？"
    await chat.observe(message(1, text))
    assert await chat.run_one()
    await age_reply(chat)
    model = FakeModel()
    restored = GroupChat(chat.db, chat.tg, chat.settings, model=model)
    await restored.recover()
    await restored.observe(message(2, text))
    assert not await restored.run_one() and not model.calls
    await restored.observe(message(3, text, user_id=43))
    assert await restored.run_one() and len(model.calls) == 1


@pytest.mark.parametrize("enabled", [False, True])
async def test_advertiser_cooldown_is_opt_in_and_never_punishes(env, enabled):
    chat, model = await configured(env, chat_advertiser_cooldown=enabled)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    await chat.observe(message(1, "果聊48r"))
    assert await chat.run_one() and len(model.calls) == 1
    model.result = "今天吃清淡一点吧。"
    await age_reply(chat)
    await chat.observe(message(2, "柴郡喵，今天吃什么晚饭？"))
    assert await chat.run_one() is (not enabled)
    assert len(model.calls) == (1 if enabled else 2)
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in chat.tg.calls)
    if enabled:
        await chat.observe(message(3, "柴郡喵，聊聊今晚吃什么？", user_id=43))
        assert await chat.run_one() and len(model.calls) == 2
        async with chat.db.transaction() as session:
            group = await session.get(Group, -1001)
            group.settings = {**group.settings, "chat_advertiser_cooldown": False}
        await age_reply(chat)
        await chat.observe(message(4, "柴郡喵，换个话题聊聊周末吧"))
        assert await chat.run_one() and len(model.calls) == 3


async def test_ai_advertising_defaults_on_and_can_be_disabled_from_telegram(env):
    from purrai.runtime import Runtime

    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, "群聊天设置")
    assert "已开启 · AI广告识别与累计处罚" in str(markup(tg))
    await click(runtime, tg, "AI广告识别与累计处罚")
    async with db.sessions() as session:
        group = await session.get(Group, -1001)
        assert group.settings["chat_advertiser_cooldown"] is False


async def test_advertiser_quiet_period_expires_and_is_limited_to_its_group(env, monkeypatch):
    from purrai import chat_commands

    chat, _ = await configured(env)
    instant = [chat_commands.now()]
    monkeypatch.setattr(chat_commands, "now", lambda: instant[0])
    async with chat.db.transaction() as session:
        await chat_commands.remember_advertiser(session, -1001, 42)
    async with chat.db.sessions() as session:
        _, state = await chat_commands.load(session, -1001)
        _, other = await chat_commands.load(session, -1002)
        assert chat_commands.advertiser_blocked(state, 42)
        assert not chat_commands.advertiser_blocked(other, 42)
    instant[0] += 1801
    async with chat.db.sessions() as session:
        _, state = await chat_commands.load(session, -1001)
        assert not chat_commands.advertiser_blocked(state, 42)
