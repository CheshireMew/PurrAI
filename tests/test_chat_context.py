"""Verify the actual model input, including late removals and nested reply content."""

import asyncio
import json

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select

from conftest import message
from purrai.archive import capture
from purrai.models import ChatMessage, ChatTurn, Violation, now
from purrai.service import enqueue
from test_chat import age_reply, configured, due


async def remove(chat, message_id, state="pending", **payload):
    async with chat.db.transaction() as session:
        task = await enqueue(
            session, "delete", f"context-remove:{message_id}", -1001,
            payload={"message_id": message_id, **payload},
        )
        task.state = state
        return task.id


@pytest.mark.parametrize("reason", ["violation", "pending", "failed", "succeeded", "archive"])
async def test_late_flagged_or_deleted_history_and_its_quote_never_reach_model(env, reason):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "这段后来被判为广告的原文"))
    if reason == "violation":
        async with chat.db.transaction() as session:
            session.add(Violation(group_id=-1001, message_id=1, user_id=42, excerpt="广告", reasons=["广告"]))
    elif reason == "archive":
        async with chat.db.transaction() as session:
            row = await capture(session, message(1, "这段后来被判为广告的原文"))
            row.deletion_state = "deleted"
    else:
        await remove(chat, 1, reason)
    await chat.observe(message(2, "柴郡喵聊聊周末吧", reply_to_message={
        "message_id": 1, "from": {"id": 42}, "text": "这段后来被判为广告的原文",
    }))
    # Upgrade compatibility: old cached quote fields must also be ignored.
    async with chat.db.transaction() as session:
        row = await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 2))
        row.reply_text = "旧缓存的广告内容"
    await due(chat)
    assert await chat.run_one()
    context = model.calls[0][1]
    assert [row["id"] for row in context["messages"]] == [2]
    assert "reply_to" not in context["messages"][0] and "quote" not in context["messages"][0]
    assert "广告" not in json.dumps(context, ensure_ascii=False)


async def test_dirty_rows_do_not_consume_clean_history_limit(env):
    chat, model = await configured(env, chat_proactive=False, chat_history=10)
    await chat.observe(message(1, "比较早的一条正常讨论", date=int(now()) - 60))
    for identifier in range(2, 14):
        await chat.observe(message(identifier, "后来确定要过滤的广告"))
        async with chat.db.transaction() as session:
            session.add(Violation(group_id=-1001, message_id=identifier, user_id=42, reasons=["广告"]))
    await chat.observe(message(14, "柴郡喵继续刚才的话题吧"))
    await due(chat)
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [1, 14]


@pytest.mark.parametrize("source", [
    {"message_id": 700, "from": {"id": 999, "is_bot": True}, "text": "验证码和管理通知"},
    {"message_id": 700, "from": {"id": 555, "is_bot": True}, "text": "其他机器人的消息"},
    {"message_id": 700, "from": {"id": 42}, "text": "/mute 42 命令内容"},
    {"message_id": 700, "from": {"id": 42}, "text": "从未单独观察过的引用原文"},
])
async def test_untrusted_embedded_quotes_are_not_a_second_input_channel(env, source):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "柴郡喵聊聊今晚吃什么", reply_to_message=source,
                               quote={"text": "额外引用的广告"}, external_reply={"text": "外群内容"}))
    await due(chat)
    assert await chat.run_one()
    context = model.calls[0][1]
    assert len(context["messages"]) == 1
    row = context["messages"][0]
    assert "quote" not in row and "reply_to" not in row
    serialized = json.dumps(context, ensure_ascii=False)
    assert source["text"] not in serialized and "外群内容" not in serialized and "额外引用的广告" not in serialized


async def test_bots_commands_service_notifications_are_excluded_but_own_chat_can_continue(env):
    chat, model = await configured(env, chat_proactive=False)
    for identifier, values in enumerate([
        {"from": {"id": 555, "is_bot": True}},
        {"from": {"id": 999, "is_bot": True}},
        {"via_bot": {"id": 555}},
        {"forward_origin": {"type": "user", "sender_user": {"id": 555, "is_bot": True}}},
        {"pinned_message": {"message_id": 700, "text": "系统通知"}},
        {"new_chat_members": [{"id": 42}]},
        {"left_chat_member": {"id": 42}},
    ], 1):
        await chat.observe(message(identifier, "柴郡喵这条是机器人或系统通知", **values))
    await chat.observe(message(8, "/admin"))
    # An old or externally inserted bot row is not proof of a PurrAI chat response.
    async with chat.db.transaction() as session:
        session.add(ChatMessage(group_id=-1001, topic_id=0, message_id=9, sender_id=999,
                                sender_name="PurrAI", text="旧系统通知", is_bot=True, sent_at=now()))
    await chat.observe(message(10, "柴郡喵今晚吃什么"))
    await due(chat)
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [10]
    async with chat.db.transaction() as session:
        turn = await session.scalar(select(ChatTurn).where(ChatTurn.state == "done"))
        sent_id = turn.sent_message_id
    await age_reply(chat)
    await chat.observe(message(11, "柴郡喵那就听你的", reply_to_message={"message_id": sent_id, "from": {"id": 999, "is_bot": True}}))
    await due(chat)
    assert await chat.run_one()
    assert any(row["id"] == sent_id and row["bot"] for row in model.calls[-1][1]["messages"])
    assert all(row["id"] != 9 for row in model.calls[-1][1]["messages"])


@pytest.mark.parametrize("reason", ["pending", "failed", "succeeded", "violation", "archive"])
async def test_already_sent_source_and_own_reply_are_removed_after_moderation(env, reason):
    chat, model = await configured(env, chat_proactive=False, archive_enabled=reason == "archive")
    await chat.observe(message(1, "柴郡喵旧的话题"))
    await due(chat)
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn).where(ChatTurn.state == "done"))
        sent_id = turn.sent_message_id
    if reason == "violation":
        async with chat.db.transaction() as session:
            session.add(Violation(group_id=-1001, message_id=1, user_id=42, reasons=["广告"]))
    elif reason == "archive":
        async with chat.db.transaction() as session:
            row = await capture(session, message(1, "柴郡喵旧的话题"))
            row.deletion_state = "deleted"
    else:
        await remove(chat, 1, reason)
    await age_reply(chat)
    await chat.observe(message(2, "柴郡喵聊个新话题"))
    await due(chat)
    assert await chat.run_one()
    ids = [row["id"] for row in model.calls[-1][1]["messages"]]
    assert ids == [2]
    assert sent_id not in ids


async def test_removal_during_typing_is_filtered_before_model_request(env):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "稍后会被删除的历史原文"))
    await chat.observe(message(2, "柴郡喵说说你的想法"))
    original = chat.tg.call

    async def call(method, **kwargs):
        if method == "send_chat_action":
            await remove(chat, 1)
        return await original(method, **kwargs)

    chat.tg.call = call
    await due(chat)
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [2]


async def test_history_removed_during_generation_cancels_unsent_answer_without_regeneration(env):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "后来被删除的历史内容"))
    await chat.observe(message(2, "柴郡喵聊聊周末"))
    await due(chat)
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        await remove(chat, 1)
    finally:
        model.release.set()
        await running
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "skipped" and "上下文" in turn.error
    assert len(model.calls) == 1


async def test_rate_limited_response_is_not_reused_after_source_removal(env):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "缓存回答引用的旧内容"))
    await chat.observe(message(2, "柴郡喵你觉得呢"))
    original, limited = chat.tg.call, False

    async def call(method, **kwargs):
        nonlocal limited
        if method == "send_message" and not limited:
            limited = True
            raise TelegramRetryAfter(method=SendMessage(chat_id=-1001, text="test"), message="wait", retry_after=1)
        return await original(method, **kwargs)

    chat.tg.call = call
    await due(chat)
    assert await chat.run_one()
    await remove(chat, 1)
    model.result = "只接着正常的新问题聊。"
    await due(chat)
    assert await chat.run_one()
    assert len(model.calls) == 2
    assert [row["id"] for row in model.calls[-1][1]["messages"]] == [2]
    sent = [values for method, values in chat.tg.calls if method == "send_message"]
    assert len(sent) == 1 and sent[0]["text"] == model.result


async def test_private_cleanup_with_same_message_id_does_not_hide_group_message(env):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "正常的群内讨论"))
    await remove(chat, 1, chat_id=42)
    await chat.observe(message(2, "柴郡喵聊聊周末"))
    await due(chat)
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [1, 2]


async def test_eligible_sent_prefix_is_unchanged_by_later_edits_and_reply_mode(env):
    chat, model = await configured(env, chat_interval=15)
    model.result = None
    await chat.observe(message(1, "周末大家一起出去走走怎么样"))
    await due(chat)
    assert await chat.run_one()
    system, first = model.calls[0]
    assert first["reply_mode"] == "ambient"
    await chat.observe(message(1, "后来被编辑的内容", edit_date=int(now()) + 1), edited=True, update_id=2)
    await chat.observe(message(2, "柴郡喵你觉得呢"))
    await due(chat)
    assert await chat.run_one()
    second = model.calls[1][1]
    assert second["reply_mode"] == "direct" and model.calls[1][0] == system
    assert second["messages"][:len(first["messages"])] == first["messages"]
    assert second["messages"][0]["text"] == "周末大家一起出去走走怎么样"
    # Everything before the old array terminator is a byte-identical reusable prefix.
    prefix = model.payloads[0].split('],"reply_to":', 1)[0]
    assert model.payloads[1].startswith(prefix)


async def test_sent_prefix_survives_restart_and_unsent_dirty_messages_are_still_filtered(env):
    from purrai.chat import GroupChat
    from test_chat import FakeModel

    chat, model = await configured(env, chat_proactive=False)
    model.result = None
    await chat.observe(message(1, "柴郡喵原来的讨论"))
    await due(chat)
    assert await chat.run_one()
    await chat.observe(message(2, "尚未发送的广告"))
    await remove(chat, 2)
    restored_model = FakeModel()
    restored = GroupChat(chat.db, chat.tg, chat.settings, model=restored_model)
    await restored.recover()
    await restored.observe(message(3, "柴郡喵接着聊吧"))
    await due(restored)
    assert await restored.run_one()
    messages = restored_model.calls[0][1]["messages"]
    assert messages[0] == model.calls[0][1]["messages"][0]
    assert [row["id"] for row in messages] == [1, 3]


async def test_prefix_rotates_in_batches_only_when_context_limit_is_reached(env):
    chat, model = await configured(env, chat_proactive=False, chat_history=10)
    model.result = None
    for identifier in range(1, 11):
        await chat.observe(message(identifier, f"正常的群友讨论 {identifier}"))
    await chat.observe(message(11, "柴郡喵轮到你说了"))
    await due(chat)
    assert await chat.run_one()
    first = model.calls[0][1]["messages"]
    assert len(first) == 10
    await chat.observe(message(12, "柴郡喵再说一句"))
    await due(chat)
    assert await chat.run_one()
    second = model.calls[1][1]["messages"]
    assert second[:-1] == first[-5:]
    await chat.observe(message(13, "柴郡喵再聊点别的"))
    await due(chat)
    assert await chat.run_one()
    third = model.calls[2][1]["messages"]
    assert third[:-1] == second
