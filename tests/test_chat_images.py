"""Unreadable images silence the sender's nearby messages without silencing the group."""

import asyncio

import pytest
from sqlalchemy import select

from conftest import message
from purrai.chat import GroupChat
from purrai.models import ChatTurn, now
from test_chat import FakeModel, configured, due


def photo(identifier, *, caption="", **kwargs):
    return message(identifier, "", photo=[{"file_id": "image", "file_unique_id": "image"}], caption=caption, **kwargs)


@pytest.mark.parametrize("media", [
    {"photo": [{"file_id": "image"}]},
    {"photo": [{"file_id": "image"}], "caption": "柴郡喵，看看这个"},
    {"document": {"file_id": "image", "mime_type": "image/png"}, "caption": "柴郡喵，这样改行吗"},
])
async def test_image_alone_or_with_caption_never_calls_model(env, media):
    chat, model = await configured(env)
    await chat.observe(message(1, "", **media))
    await due(chat)
    assert not await chat.run_one() and not model.calls


@pytest.mark.parametrize("image_first", [True, False])
async def test_nearby_photo_and_text_are_silent_in_either_order_even_when_addressed(env, image_first):
    chat, model = await configured(env)
    start = int(now()) - 10
    messages = [photo(1, date=start), message(2, "柴郡喵，这样改可行吗？", date=start + 2)]
    if not image_first:
        messages = [message(1, "柴郡喵，这样改可行吗？", date=start), photo(2, date=start + 2)]
    for item in messages:
        await chat.observe(item)
    await due(chat)
    assert not await chat.run_one() and not model.calls
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "skipped" and "图片" in turn.error


@pytest.mark.parametrize("separation,expected", [(10, False), (11, True)])
async def test_image_window_uses_actual_send_times_and_expires(env, separation, expected):
    chat, model = await configured(env)
    await chat.observe(photo(1, date=int(now()) - separation))
    await chat.observe(message(2, "柴郡喵，晚饭吃点什么？"))
    await due(chat)
    assert await chat.run_one() is expected
    assert bool(model.calls) is expected
    if model.calls:
        assert [row["id"] for row in model.calls[0][1]["messages"]] == [2]


@pytest.mark.parametrize("unrelated", ["sender", "topic"])
async def test_other_people_or_other_forum_topics_do_not_silence_a_normal_question(env, unrelated):
    chat, model = await configured(env)
    if unrelated == "sender":
        image = photo(1, user_id=43)
        question = message(2, "柴郡喵，晚饭吃点什么？", user_id=42)
    else:
        image = photo(1, is_topic_message=True, message_thread_id=10)
        question = message(2, "柴郡喵，晚饭吃点什么？", is_topic_message=True, message_thread_id=20)
    await chat.observe(image)
    await chat.observe(question)
    await due(chat)
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [2]


async def test_reply_to_an_unseen_image_is_also_silent(env):
    chat, model = await configured(env)
    await chat.observe(message(2, "柴郡喵，这样改可行吗？", reply_to_message=photo(1)))
    await due(chat)
    assert not await chat.run_one() and not model.calls


async def test_photo_arriving_during_generation_cancels_text_and_sticker(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，这样改可行吗？"))
    await due(chat)
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        await chat.observe(photo(2))
    finally:
        model.release.set()
        await running
    assert len(model.calls) == 1
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)


async def test_restart_preserves_image_boundary_and_other_members_can_still_chat(env):
    chat, _ = await configured(env)
    await chat.observe(photo(1))
    restored_model = FakeModel()
    restored = GroupChat(chat.db, chat.tg, chat.settings, model=restored_model)
    await restored.recover()
    await restored.observe(message(2, "柴郡喵，这样改可行吗？"))
    await restored.observe(message(3, "柴郡喵，晚饭吃点什么？", user_id=43))
    await due(restored)
    assert await restored.run_one()
    assert [row["id"] for row in restored_model.calls[0][1]["messages"]] == [3]
    assert restored_model.calls[0][1]["reply_to"] == 3
