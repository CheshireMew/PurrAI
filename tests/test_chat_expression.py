import asyncio
import json

import httpx
import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendSticker
from sqlalchemy import select

from conftest import message
from purrai.chat import GroupChat
from purrai.chat_expression import separate_emoji
from purrai.chat_model import ChatModel, ChatModelError, ChatReply
from purrai.config import Settings
from purrai.management import Management
from purrai.models import ArchivedMessage, ChatMessage, ChatTurn, Group, Meta, now
from test_chat import age_reply, configured, due
from test_stickers import cache, item


@pytest.mark.parametrize("emoji", ["🐱", "😆", "☕️", "❤", "👩‍❤️‍👩", "👨🏿‍🎓", "🏳️‍🌈", "🇨🇳", "1️⃣", "🙂‍↔️", "🫪"])
def test_emoji_sequences_are_separated_whole(emoji):
    assert separate_emoji(f"你好 {emoji}，再聊") == ("你好，再聊", [emoji])


def test_numbers_plain_symbols_and_text_are_preserved():
    text = "12345 #tag *内容* 1+2=3 ©™®，中文 English。"
    assert separate_emoji(text) == (text, [])
    assert separate_emoji("🐱\n😆") == (None, ["🐱", "😆"])


async def test_inline_emoji_becomes_separate_sticker_and_both_outputs_are_saved(env):
    chat, model = await configured(env)
    model.result = ChatReply("我也觉得挺好 🐱")
    await cache(chat, items=[item(1, "🐱")])
    await chat.observe(message(1, "柴郡喵你觉得呢", message_thread_id=77, is_topic_message=True))
    await due(chat)
    await chat.run_one()
    outgoing = [(kind, data) for kind, data in chat.tg.calls if kind in ("send_message", "send_sticker")]
    assert [kind for kind, _ in outgoing] == ["send_message", "send_sticker"]
    assert outgoing[0][1]["text"] == "我也觉得挺好"
    assert all(data["message_thread_id"] == 77 and data["reply_parameters"]["message_id"] == 1 for _, data in outgoing)
    assert len(model.calls) == 1 and (await chat.budget.status(-1001))["group_calls"] == 1
    async with chat.db.sessions() as session:
        own = (await session.scalars(select(ChatMessage).where(ChatMessage.is_bot).order_by(ChatMessage.id))).all()
        assert [row.text for row in own] == ["我也觉得挺好", "[贴纸 🐱]"]
        assert len((await session.scalars(select(ArchivedMessage))).all()) == 2
    async with chat.db.transaction() as session:
        row = await session.get(Meta, "chat_session:-1001:77")
        value = json.loads(row.value)
        value.update(last_reply=now() - 10, last_attempt=now() - 10)
        row.value = json.dumps(value)
    await chat.observe(message(2, "柴郡喵接着聊", message_thread_id=77, is_topic_message=True))
    await due(chat)
    await chat.run_one()
    assert [row["text"] for row in model.calls[-1][1]["messages"] if row["bot"]] == ["我也觉得挺好", "[贴纸 🐱]"]


async def test_negative_participation_decision_sends_neither_text_nor_sticker(env):
    chat, model = await configured(env)
    await cache(chat)
    model.result = ChatReply("这个内容不应该发出 😸", "😸", should_reply=False)
    await chat.observe(message(1, "大家在聊自己的事"))
    await due(chat)
    await chat.run_one()
    assert not any(kind in ("send_message", "send_sticker") for kind, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "silent"


@pytest.mark.parametrize("decision", [None, "yes", 1])
async def test_model_response_without_boolean_participation_is_rejected(decision):
    body = {"reply": "你好 🐱", **({"should_reply": decision} if decision is not None else {})}
    model = ChatModel(Settings(_env_file=None), httpx.MockTransport(lambda request: httpx.Response(200,
        json={"choices": [{"message": {"content": json.dumps(body)}}]})))
    try:
        with pytest.raises(ChatModelError):
            await model.reply("先判断是否需要说话", "上下文")
    finally:
        await model.close()


async def test_model_negative_decision_ignores_accidental_reply_content():
    model = ChatModel(Settings(_env_file=None), httpx.MockTransport(lambda request: httpx.Response(200,
        json={"choices": [{"message": {"content": json.dumps({"should_reply": False, "reply": "不要发出", "sticker": "🐱"})}}],
              "usage": {"total_tokens": 12}})))
    try:
        assert await model.reply("先判断是否需要说话", "上下文") == ChatReply(total_tokens=12, should_reply=False)
    finally:
        await model.close()


async def test_ambient_checks_share_cooldown_across_topics_and_can_resume(env):
    chat, model = await configured(env, chat_interval=90)
    model.result = ChatReply(should_reply=False)
    await chat.observe(message(1, "大家在讨论周末活动", message_thread_id=77, is_topic_message=True))
    await chat.observe(message(2, "另一个话题的群友闲聊", message_thread_id=88, is_topic_message=True))
    await due(chat)
    await asyncio.gather(chat.run_one(), chat.run_one())
    assert len(model.calls) == 1
    async with chat.db.transaction() as session:
        row = await session.get(Meta, "chat_attention:-1001")
        row.value = json.dumps({"last_attempt": now() - 100})
    model.result = ChatReply("这个建议不错，可以试试。")
    await chat.observe(message(3, "大家有没有合适的活动建议", user_id=43, message_thread_id=99, is_topic_message=True))
    await due(chat)
    await chat.run_one()
    assert len(model.calls) == 2 and model.calls[-1][1]["reply_mode"] == "ambient"
    assert [data["text"] for kind, data in chat.tg.calls if kind == "send_message"] == ["这个建议不错，可以试试。"]


async def test_recent_user_replying_to_someone_else_does_not_force_followup(env):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "柴郡喵你好"))
    await due(chat)
    await chat.run_one()
    await age_reply(chat)
    reply = {"message_id": 50, "from": {"id": 43, "is_bot": False}, "text": "问别人的问题"}
    await chat.observe(message(2, "那你先去忙吧", reply_to_message=reply))
    await due(chat)
    assert not await chat.run_one() and len(model.calls) == 1
    await chat.observe(message(3, "柴郡喵你觉得如何", reply_to_message=reply))
    await due(chat)
    await chat.run_one()
    assert len(model.calls) == 2 and model.calls[-1][1]["reply_mode"] == "direct"
    assert model.calls[-1][1]["reply_audience"] == "other"


@pytest.mark.parametrize("direct", [True, False])
async def test_sticker_retry_after_text_success_does_not_repeat_text_or_model(env, direct):
    chat, model = await configured(env)
    await cache(chat)
    model.result = ChatReply("先聊文字 😸", "😸")
    original, attempts = chat.tg.call, []

    async def limited(kind, **kwargs):
        if kind == "send_sticker":
            attempts.append(kwargs["sticker"])
            if len(attempts) == 1:
                raise TelegramRetryAfter(method=SendSticker(chat_id=-1001, sticker="file-1"), message="wait", retry_after=1)
        return await original(kind, **kwargs)

    chat.tg.call = limited
    await chat.observe(message(1, "柴郡喵你好呀" if direct else "这个周末活动的建议挺不错的",
                               reply_to_message={"message_id": 50, "from": {"id": 43, "is_bot": False}}))
    await due(chat)
    await chat.run_one()
    restored = GroupChat(chat.db, chat.tg, chat.settings, model=model)
    await restored.recover()
    await due(restored)
    await restored.run_one()
    assert [data["text"] for kind, data in chat.tg.calls if kind == "send_message"] == ["先聊文字"]
    assert attempts == ["file-1", "file-1"] and len(model.calls) == 1
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "done"


@pytest.mark.parametrize("revoke", [False, True])
async def test_text_delivery_then_settings_change_suppresses_following_sticker(env, revoke):
    chat, model = await configured(env)
    await cache(chat)
    model.result = ChatReply("已经发出的文字", "😸")
    original = chat.tg.call

    async def change(kind, **kwargs):
        sent = await original(kind, **kwargs)
        if kind == "send_message":
            if revoke:
                management = Management(chat.db, chat.tg, env[3], chat.settings)
                await management.group_access(1, -1001, False)
            else:
                async with chat.db.transaction() as session:
                    group = await session.get(Group, -1001)
                    group.settings = {**group.settings, "chat_stickers": False}
        return sent

    chat.tg.call = change
    await chat.observe(message(1, "柴郡喵你好"))
    await due(chat)
    await chat.run_one()
    assert len([1 for kind, _ in chat.tg.calls if kind == "send_message"]) == 1
    assert not any(kind == "send_sticker" for kind, _ in chat.tg.calls)
    assert len(model.calls) == 1
