"""Fast, unmentioned replies must reach the conversation model after pacing."""

import pytest
from sqlalchemy import select

from conftest import message
from purrai.chat import GroupChat
from purrai.chat_model import ChatReply
from purrai.models import ChatTurn, now
from test_chat import age_reply, configured, due


async def offer_joke(env):
    chat, model = await configured(env, chat_proactive=False)
    model.result = "那我给你讲个冷笑话，冷到需要盖被子那种。"
    await chat.observe(message(1, "柴郡喵，无聊了，随便说点啥。"))
    await due(chat)
    assert await chat.run_one()
    return chat, model


@pytest.mark.parametrize("text", ["什么冷笑话", "讲吧", "然后呢", "讲"])
async def test_immediate_unmentioned_followup_waits_instead_of_disappearing(env, text):
    chat, model = await offer_joke(env)
    await chat.observe(message(2, text))
    async with chat.db.sessions() as session:
        queued = await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 2))
        assert queued is not None and queued.state == "pending"
    assert not await chat.run_one()
    assert len(model.calls) == 1

    # Advance the conversation cooldown, without making the member send again.
    await age_reply(chat)
    await due(chat)
    model.result = "小明剪了个中分，结果变成了小日月。"
    assert await chat.run_one()
    request = model.calls[-1][1]
    assert request["reply_mode"] == "followup" and request["reply_to"] == 2
    assert any(row["bot"] and "冷笑话" in row["text"] for row in request["messages"])
    assert request["messages"][-1]["text"] == text
    outgoing = [data["text"] for method, data in chat.tg.calls if method == "send_message"]
    assert outgoing[-1] == model.result and len(outgoing) == 2


async def test_fast_followups_merge_and_survive_restart_without_a_second_mention(env):
    chat, model = await offer_joke(env)
    await chat.observe(message(2, "什么冷笑话"))
    await chat.observe(message(3, "别卖关子了，直接讲吧"))
    restored = GroupChat(chat.db, chat.tg, chat.settings, model=model)
    await restored.recover()
    await age_reply(restored)
    await due(restored)
    assert await restored.run_one()
    assert not await restored.run_one()
    assert len(model.calls) == 2
    request = model.calls[-1][1]
    assert request["reply_mode"] == "followup" and request["reply_to"] == 3
    assert [row["text"] for row in request["messages"]][-2:] == ["什么冷笑话", "别卖关子了，直接讲吧"]
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 2))).state == "merged"


@pytest.mark.parametrize("values", [
    {"user_id": 43},
    {"message_thread_id": 77},
    {"reply_to_message": {"message_id": 500, "from": {"id": 43, "is_bot": False}}},
])
async def test_continuation_does_not_capture_other_people_or_topics(env, values):
    chat, model = await offer_joke(env)
    await age_reply(chat)
    await chat.observe(message(2, "那你接着讲吧", **values))
    await due(chat)
    assert not await chat.run_one()
    assert len(model.calls) == 1


async def test_model_can_still_decline_an_unrelated_followup(env):
    chat, model = await offer_joke(env)
    await chat.observe(message(2, "我先接个电话，你们聊"))
    await age_reply(chat)
    await due(chat)
    model.result = ChatReply(should_reply=False)
    assert await chat.run_one()
    assert len(model.calls) == 2 and model.calls[-1][1]["reply_mode"] == "followup"
    assert len([1 for method, _ in chat.tg.calls if method == "send_message"]) == 1
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 2))).state == "silent"


async def test_followup_received_within_window_is_not_lost_while_waiting_for_worker(env, monkeypatch):
    chat, model = await offer_joke(env)
    await age_reply(chat, seconds=89)
    await chat.observe(message(2, "讲吧"))
    later = now() + 5
    monkeypatch.setattr("purrai.chat.now", lambda: later)
    await due(chat)
    assert await chat.run_one()
    assert len(model.calls) == 2 and model.calls[-1][1]["reply_mode"] == "followup"


async def test_old_conversation_does_not_trigger_without_proactive_chat(env):
    chat, model = await offer_joke(env)
    await age_reply(chat, seconds=91)
    await chat.observe(message(2, "什么冷笑话"))
    await due(chat)
    assert not await chat.run_one() and len(model.calls) == 1


async def test_followup_after_model_silence_waits_for_request_cooldown(env):
    chat, model = await offer_joke(env)
    await age_reply(chat)
    model.result = ChatReply(should_reply=False)
    await chat.observe(message(2, "我先回个消息"))
    await due(chat)
    assert await chat.run_one()
    await chat.observe(message(3, "好了，你接着讲"))
    await due(chat)
    assert not await chat.run_one() and len(model.calls) == 2
    await age_reply(chat)
    await due(chat)
    model.result = "那接着讲。"
    assert await chat.run_one() and len(model.calls) == 3
