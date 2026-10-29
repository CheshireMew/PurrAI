"""Explicit questions must survive unreadable images and keep the referenced text."""

import json

import pytest
from sqlalchemy import select

from conftest import message
from purrai.archive import capture
from purrai.models import ChatTurn, now
from test_chat import configured
from test_chat_context import remove
from test_chat_images import photo


def channel_post(identifier=700, *, image=True):
    return {
        "message_id": identifier, "date": int(now()),
        "chat": {"id": -1001, "type": "supergroup"},
        "sender_chat": {"id": -100900, "type": "channel", "title": "群日报"},
        "is_automatic_forward": True,
        **({"photo": [{"file_id": "photo"}], "caption": "新功能下周上线，大家想拿它做什么？"} if image else
           {"text": "新功能下周上线，大家想拿它做什么？"}),
    }


async def test_direct_question_is_immediately_runnable_without_a_fixed_wait(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，聊聊今晚吃什么"))
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.available_at <= now()
    assert await chat.run_one() and len(model.calls) == 1


@pytest.mark.parametrize("image", [True, False])
@pytest.mark.parametrize("archive_enabled", [True, False])
async def test_direct_reference_to_channel_post_uses_caption_without_triggering_on_channel_traffic(env, image, archive_enabled):
    chat, model = await configured(env, archive_enabled=archive_enabled)
    source = channel_post(image=image)
    async with chat.db.transaction() as session:
        await capture(session, source)
    await chat.observe(source)
    assert not await chat.run_one() and not model.calls
    await chat.observe(message(1, "@purrai_bot 你怎么看？", reply_to_message=source))
    assert await chat.run_one()
    entry = next(row for row in model.calls[0][1]["messages"] if row["id"] == 1)
    assert entry["quote"] == "新功能下周上线，大家想拿它做什么？" and entry["reply_to"] == 700
    assert entry.get("image_unavailable", False) is image
    assert "reply_mode" not in model.calls[0][1]
    assert "file_id" not in json.dumps(model.calls[0][1])


@pytest.mark.parametrize("addressing", ["name", "mention", "reply"])
async def test_direct_image_caption_or_reply_to_bot_is_answered(env, addressing):
    chat, model = await configured(env)
    values = {"caption": "柴郡喵，这张图怎样？"} if addressing == "name" else {
        "caption": "@purrai_bot 这张图怎样？"} if addressing == "mention" else {
        "reply_to_message": {"message_id": 800, "from": {"id": 999, "is_bot": True}},
    }
    await chat.observe(photo(1, **values))
    assert await chat.run_one()
    entry = model.calls[0][1]["messages"][-1]
    assert entry["image_unavailable"] is True
    assert "reply_mode" not in model.calls[0][1]


async def test_direct_reply_can_use_another_members_image_caption(env):
    chat, model = await configured(env)
    source = photo(1, user_id=43, caption="我做的木制书架，用松木拼接")
    await chat.observe(source)
    await chat.observe(message(2, "@purrai_bot 你觉得牢固吗？", reply_to_message=source))
    assert await chat.run_one()
    target = next(row for row in model.calls[0][1]["messages"] if row["id"] == 2)
    assert target["quote"] == "我做的木制书架，用松木拼接" and target["image_unavailable"]


async def test_filtered_image_caption_keeps_image_limit_when_user_asks_for_advice(env):
    chat, model = await configured(env)
    source = channel_post()
    source["caption"] = "带单返佣，看我简介私聊"
    await chat.observe(message(1, "@purrai_bot 怎么识别这类广告？", reply_to_message=source))
    assert await chat.run_one()
    target = next(row for row in model.calls[0][1]["messages"] if row["id"] == 1)
    assert target["image_unavailable"] and not target["quote"]
    assert source["caption"] not in json.dumps(model.calls[0][1], ensure_ascii=False)


@pytest.mark.parametrize("values", [
    {"from": {"id": 55, "is_bot": True}},
    {"via_bot": {"id": 55, "is_bot": True}},
    {"forward_origin": {"type": "user", "sender_user": {"id": 55, "is_bot": True}}},
])
async def test_bot_system_image_quotes_do_not_enter_the_new_reference_path(env, values):
    chat, model = await configured(env)
    source = photo(700, caption="其他机器人的系统广告内容", **values)
    await chat.observe(message(1, "@purrai_bot 聊聊周末", reply_to_message=source))
    assert await chat.run_one()
    assert "其他机器人的系统广告内容" not in json.dumps(model.calls[0][1], ensure_ascii=False)


async def test_latest_observed_channel_caption_takes_priority_over_old_reply_snapshot(env):
    chat, model = await configured(env)
    source = channel_post()
    async with chat.db.transaction() as session:
        await capture(session, {**source, "caption": "更新：工具延期到下个月发布"})
    await chat.observe(message(1, "@purrai_bot 你怎么看？", reply_to_message=source))
    assert await chat.run_one()
    entry = model.calls[0][1]["messages"][-1]
    assert entry["quote"] == "更新：工具延期到下个月发布"


async def test_deleted_channel_reference_does_not_reenter_model_through_snapshot(env):
    chat, model = await configured(env)
    source = channel_post()
    await remove(chat, source["message_id"])
    await chat.observe(message(1, "@purrai_bot 你怎么看？", reply_to_message=source))
    assert await chat.run_one()
    entry = model.calls[0][1]["messages"][-1]
    assert "quote" not in entry
    assert "新功能下周上线" not in json.dumps(model.calls[0][1], ensure_ascii=False)


async def test_deleted_channel_quote_cancels_generation_before_delivery(env):
    import asyncio

    chat, model = await configured(env)
    await chat.observe(message(1, "@purrai_bot 你怎么看？", reply_to_message=channel_post()))
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        await remove(chat, 700)
    finally:
        model.release.set()
        await running
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)
