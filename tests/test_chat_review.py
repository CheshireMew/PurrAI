"""Conversation failures reproduced from ordinary Telegram reply chains."""

import json
import logging

import pytest
from sqlalchemy import select

from conftest import message
from purrai.chat_model import ChatReply
from purrai.models import ChatMessage, ChatTurn, Meta, now
from test_chat import age_reply, configured, due


@pytest.mark.parametrize("topic_flag", [None, False])
async def test_ordinary_reply_chain_and_plain_followup_share_one_conversation(env, topic_flag):
    chat, model = await configured(env, chat_proactive=False)
    model.result = "那我给你讲个冷笑话。"
    flags = {} if topic_flag is None else {"is_topic_message": topic_flag}
    await chat.observe(message(
        1, "无聊了，随便说点啥", message_thread_id=700,
        reply_to_message={"message_id": 800, "from": {"id": 999, "is_bot": True}}, **flags,
    ))
    await due(chat)
    assert await chat.run_one()
    sent = [data for method, data in chat.tg.calls if method == "send_message"]
    assert "message_thread_id" not in sent[0]
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).topic_id == 0
    await age_reply(chat)
    model.result = "小明剪了中分，成了小日月。"
    await chat.observe(message(2, "什么冷笑话"))
    await due(chat)
    assert await chat.run_one()
    assert model.calls[-1][1]["reply_mode"] == "followup"
    assert any(row["bot"] and "冷笑话" in row["text"] for row in model.calls[-1][1]["messages"])


async def test_another_member_can_join_recent_conversation_without_a_mention(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，随便讲点什么"))
    await due(chat)
    assert await chat.run_one()
    await age_reply(chat)
    await chat.observe(message(2, "我也想听，你接着讲", user_id=43))
    await due(chat)
    assert await chat.run_one()
    request = model.calls[-1][1]
    assert len(model.calls) == 2 and request["reply_mode"] == "followup"
    assert request["reply_to"] == 2 and any(row["bot"] for row in request["messages"])
    assert len([1 for method, _ in chat.tg.calls if method == "send_chat_action"]) == 2


async def test_new_participant_does_not_replace_an_existing_followup_in_a_batch(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，讲点什么"))
    await due(chat)
    assert await chat.run_one()
    await age_reply(chat)
    await chat.observe(message(2, "什么冷笑话"))
    await chat.observe(message(3, "我下楼拿个快递", user_id=43))
    await due(chat)
    assert await chat.run_one()
    assert model.calls[-1][1]["reply_to"] == 2
    assert [row["id"] for row in model.calls[-1][1]["messages"]][-2:] == [2, 3]
    assert not await chat.run_one()


async def test_active_conversation_does_not_bypass_explicit_reply_to_someone_else(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，讲点什么"))
    await due(chat)
    assert await chat.run_one()
    await age_reply(chat)
    await chat.observe(message(2, "你几点出门", user_id=43,
                               reply_to_message={"message_id": 500, "from": {"id": 44}}))
    await due(chat)
    assert not await chat.run_one() and len(model.calls) == 1


async def test_short_throttle_defers_a_direct_question_without_spending_a_model_call(env, caplog):
    caplog.set_level(logging.INFO, logger="purrai")
    chat, model = await configured(env)
    chat.settings.chat_user_interval = 5
    await chat.budget.reserve(-1001, 42, "system", "context")
    await chat.observe(message(1, "柴郡喵，我有个问题"))
    await due(chat)
    assert await chat.run_one()
    async with chat.db.transaction() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "pending" and turn.available_at > now()
        row = await session.get(Meta, "private:chat-budget")
        budget = json.loads(row.value)
        assert budget["global"]["calls"] == 1
        budget["users"]["42"]["last_attempt"] = now() - 6
        row.value = json.dumps(budget)
    assert not model.calls
    assert "聊天等待" in caplog.text and "调用间隔未到" in caplog.text
    assert "聊天请求模型" not in caplog.text
    await due(chat)
    assert await chat.run_one() and len(model.calls) == 1


async def test_joining_a_conversation_still_allows_model_silence(env, caplog):
    caplog.set_level(logging.INFO, logger="purrai")
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，你觉得呢"))
    await due(chat)
    assert await chat.run_one()
    await age_reply(chat)
    caplog.clear()
    model.result = ChatReply(should_reply=False)
    await chat.observe(message(2, "我先出去一趟", user_id=43))
    await due(chat)
    assert await chat.run_one()
    assert len(model.calls) == 2
    assert len([1 for method, _ in chat.tg.calls if method == "send_message"]) == 1
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 2))
        assert turn.state == "silent" and turn.error == "模型判断无需回复"
        assert len((await session.scalars(select(ChatMessage).where(ChatMessage.is_bot))).all()) == 1
    assert "聊天请求模型" in caplog.text and "模型判断无需回复" in caplog.text
    assert "我先出去一趟" not in caplog.text


async def test_filtered_message_explains_why_it_did_not_reach_model(env, caplog):
    caplog.set_level(logging.INFO, logger="purrai")
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "朋友们晚上好"))
    assert not await chat.run_one() and not model.calls
    assert "聊天未进入模型" in caplog.text and "主动聊天关闭" in caplog.text
    assert "message=1" in caplog.text and "朋友们晚上好" not in caplog.text


@pytest.mark.parametrize("failure_kind", ["quota", "send"])
async def test_quota_and_delivery_failure_have_distinct_diagnostics(env, caplog, failure_kind):
    caplog.set_level(logging.INFO, logger="purrai")
    chat, model = await configured(env)
    if failure_kind == "quota":
        chat.settings.chat_daily_calls = 0
    else:
        env[2].fail["send_message"] = 1
    await chat.observe(message(1, "柴郡喵，讲个笑话"))
    await due(chat)
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        if failure_kind == "quota":
            assert turn.state == "skipped" and "今日 AI 聊天额度已用完" in turn.error
            assert not model.calls and "聊天未请求模型" in caplog.text
        else:
            assert turn.state == "failed" and "ConnectionError" in turn.error
            assert len(model.calls) == 1 and "聊天回复失败" in caplog.text
            assert "message=1" in caplog.text
    assert "柴郡喵，讲个笑话" not in caplog.text
