"""End-to-end conversation cases found by reviewing the complete chat flow."""

import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage, SendSticker
from sqlalchemy import select

from conftest import message
from purrai.archive import capture
from purrai.chat_model import ChatReply
from purrai.chat_state import following_up
from purrai.models import ArchivedMessage, ChatMessage, ChatTurn, now
from test_chat import age_reply, configured, due
from test_chat_context import remove
from test_chat_images import photo
from test_stickers import cache


async def test_queued_supplements_merge_without_proactive_chat_or_artificial_wait(env, monkeypatch):
    chat, model = await configured(env, chat_proactive=False)
    clock = [now()]
    monkeypatch.setattr("purrai.chat.now", lambda: clock[0])
    monkeypatch.setattr("purrai.chat_context.now", lambda: clock[0])
    await chat.observe(message(1, "柴郡喵，帮我想想周末去哪玩", date=int(clock[0])))
    clock[0] += 6
    await chat.observe(message(2, "最好近一点，周日晚上要回来", date=int(clock[0])))
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [1, 2]
    assert not await chat.run_one()


async def test_two_members_direct_questions_are_not_merged_into_one_answer(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，周末去哪里玩？", user_id=42))
    await chat.observe(message(2, "柴郡喵，我想煮面条，怎么做？", user_id=43))
    await due(chat)
    assert await chat.run_one()
    assert model.calls[0][1]["reply_to"] == 1
    assert 2 not in [row["id"] for row in model.calls[0][1]["messages"]]
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 2))).state == "pending"
    await age_reply(chat)
    await due(chat)
    assert await chat.run_one()
    assert model.calls[-1][1]["reply_to"] == 2 and len(model.calls) == 2


async def test_deleted_candidate_does_not_swallow_another_members_valid_question(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，给我讲个冷笑话", user_id=42))
    await chat.observe(message(2, "柴郡喵，这条稍后删除", user_id=43))
    await remove(chat, 2)
    await due(chat)
    assert await chat.run_one()
    assert model.calls[0][1]["reply_to"] == 1


async def test_supplement_during_generation_is_queued_even_with_proactive_chat_disabled(env, monkeypatch):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "柴郡喵，周末去哪里玩"))
    await due(chat)
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        await chat.observe(message(2, "补充一下，我只有半天时间"))
    finally:
        model.release.set()
        await running
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 2))
        assert turn is not None and turn.state == "pending"
    later = now() + 11
    monkeypatch.setattr("purrai.chat.now", lambda: later)
    monkeypatch.setattr("purrai.chat_context.now", lambda: later)
    await due(chat)
    assert await chat.run_one() and len(model.calls) == 2
    assert model.calls[-1][1]["reply_to"] == 2
    assert model.calls[-1][1]["reply_mode"] == "followup"


@pytest.mark.parametrize("values", [
    {"user_id": 43},
    {"is_topic_message": True, "message_thread_id": 77},
    {"reply_to_message": {"message_id": 500, "from": {"id": 43}}},
])
async def test_inflight_request_does_not_capture_other_people_topics_or_explicit_replies(env, values):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "柴郡喵，周末去哪里玩"))
    await due(chat)
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        await chat.observe(message(2, "补充一下，我只有半天时间", **values))
    finally:
        model.release.set()
        await running
    async with chat.db.sessions() as session:
        assert await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 2)) is None


@pytest.mark.parametrize("archive_enabled", [False, True])
async def test_missing_telegram_reply_target_never_falls_back_to_a_standalone_message(env, archive_enabled):
    chat, model = await configured(env, archive_enabled=archive_enabled)
    await chat.observe(message(1, "柴郡喵，这条已经被其他管理员删除"))
    if archive_enabled:
        async with chat.db.transaction() as session:
            await capture(session, message(1, "柴郡喵，这条已经被其他管理员删除"))
    original, attempts = chat.tg.call, []

    async def telegram(method, **kwargs):
        if method == "send_message" and kwargs.get("reply_parameters", {}).get("message_id") == 1:
            attempts.append(kwargs)
            if not kwargs["reply_parameters"].get("allow_sending_without_reply"):
                raise TelegramBadRequest(method=SendMessage(**kwargs), message="message to be replied not found")
        return await original(method, **kwargs)

    chat.tg.call = telegram
    await due(chat)
    assert await chat.run_one()
    assert len(attempts) == 1
    assert not any(method == "send_message" for method, _ in chat.tg.calls)
    async with chat.db.transaction() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "skipped" and "不存在" in turn.error
        if archive_enabled:
            archived = await session.scalar(select(ArchivedMessage))
            assert archived.deletion_state == "already_absent"
        # The tiny rolling row can age out. Confirmed absence must remain known.
        row = await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 1))
        row.message_id = 9000
    await chat.observe(message(2, "柴郡喵，换个正常话题吧", user_id=43))
    await due(chat)
    assert await chat.run_one()
    assert 1 not in [row["id"] for row in model.calls[-1][1]["messages"]]


async def test_missing_reply_target_does_not_disable_a_working_sticker_pack(env):
    chat, model = await configured(env)
    await cache(chat)
    model.result = ChatReply(sticker="😸")
    original = chat.tg.call

    async def telegram(method, **kwargs):
        if method == "send_sticker":
            raise TelegramBadRequest(method=SendSticker(**kwargs), message="message to be replied not found")
        return await original(method, **kwargs)

    chat.tg.call = telegram
    await chat.observe(message(1, "柴郡喵，发个表情"))
    await due(chat)
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "skipped"
        _, state = await chat.stickers.state(session, -1001)
        assert not state.get("blocked_until")


async def test_sticker_retry_keeps_original_sources_and_cancels_after_history_deletion(env):
    chat, model = await configured(env, chat_proactive=False)
    await cache(chat)
    model.result = ChatReply("先聊聊这个正常问题。", "😸")
    await chat.observe(message(1, "后来会被清理的一条历史消息"))
    await chat.observe(message(2, "柴郡喵，你怎么看？"))
    original, attempts = chat.tg.call, []

    async def telegram(method, **kwargs):
        if method == "send_sticker":
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise TelegramRetryAfter(method=SendSticker(**kwargs), message="wait", retry_after=1)
        return await original(method, **kwargs)

    chat.tg.call = telegram
    await due(chat)
    assert await chat.run_one()
    await remove(chat, 1)
    await due(chat)
    assert await chat.run_one()
    assert len(model.calls) == 1 and len(attempts) == 1
    assert len([1 for method, _ in chat.tg.calls if method == "send_message"]) == 1


@pytest.mark.parametrize("offset,expected", [(-1, False), (0, True), (89, True), (90, False)])
def test_messages_received_before_bot_reply_are_not_its_followups(offset, expected):
    assert following_up({"last_reply": 100, "last_user": 42}, 42, 100 + offset) is expected


async def test_image_silences_the_entire_time_connected_burst_not_just_nearest_message(env, monkeypatch):
    chat, model = await configured(env)
    clock = [now()]
    monkeypatch.setattr("purrai.chat.now", lambda: clock[0])
    monkeypatch.setattr("purrai.chat_context.now", lambda: clock[0])
    await chat.observe(message(1, "这样改可行吗", date=int(clock[0])))
    clock[0] += 8
    await chat.observe(message(2, "还有这部分，你也帮我看看", date=int(clock[0])))
    clock[0] += 8
    await chat.observe(photo(3, date=int(clock[0])))
    clock[0] += 11
    assert not await chat.run_one() and not model.calls


async def test_ad_aside_during_unrelated_followup_is_silent_but_explicit_ad_conversation_continues(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，周末去哪玩"))
    await due(chat)
    assert await chat.run_one()
    await age_reply(chat)
    await chat.observe(message(2, "广告又多了啊"))
    await due(chat)
    assert not await chat.run_one() and len(model.calls) == 1
    await chat.observe(message(3, "柴郡喵，怎么辨别广告里的骗局？"))
    await due(chat)
    assert await chat.run_one()
    await age_reply(chat)
    await chat.observe(message(4, "那这种广告有什么明显特征？"))
    await due(chat)
    assert await chat.run_one()
    assert model.calls[-1][1]["reply_mode"] == "followup" and len(model.calls) == 3


async def test_sticker_does_not_erase_the_explicit_ad_conversation(env):
    chat, model = await configured(env)
    await cache(chat)
    model.result = ChatReply("可以先检查来源和承诺。", "😸")
    await chat.observe(message(1, "柴郡喵，怎么辨别广告里的骗局？"))
    await due(chat)
    assert await chat.run_one()
    assert any(method == "send_sticker" for method, _ in chat.tg.calls)
    await age_reply(chat)
    await chat.observe(message(2, "那这种广告还有什么明显特征？"))
    await due(chat)
    assert await chat.run_one() and len(model.calls) == 2
    assert model.calls[-1][1]["reply_mode"] == "followup"


async def test_ad_aside_during_generation_is_not_promoted_into_a_direct_question(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，周末去哪里玩"))
    await due(chat)
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        await chat.observe(message(2, "广告又多了啊"))
    finally:
        model.release.set()
        await running
    await age_reply(chat)
    await due(chat)
    assert not await chat.run_one() and len(model.calls) == 1
