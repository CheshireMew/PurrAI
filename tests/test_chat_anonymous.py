import copy
import json

import pytest
from sqlalchemy import select

from conftest import message
from purrai.chat import GroupChat
from purrai.chat_state import chat_sender, human_message
from purrai.models import ArchivedMessage, ChatMessage, ChatTurn, Group, Meta, default_settings
from test_chat import FakeModel, age_reply, configured


def anonymous(identifier, text, *, group_id=-1001, **kwargs):
    return message(identifier, text, **{
        "chat": {"id": group_id, "type": "supergroup", "title": "测试群"},
        "from": {"id": 1087968824, "is_bot": True, "first_name": "Group", "username": "GroupAnonymousBot"},
        "sender_chat": {"id": group_id, "type": "supergroup", "title": "测试群"},
        **kwargs,
    })


@pytest.mark.parametrize("addressing", ["mention", "reply", "missing_from"])
async def test_anonymous_direct_chat_uses_group_identity_and_replies_to_message(env, addressing):
    chat, model = await configured(env, chat_proactive=False)
    _, db, tg, service = env
    source = anonymous(1, "@purrai_bot 宝宝泥嚎")
    if addressing == "reply":
        source.update(text="宝宝泥嚎", reply_to_message={"message_id": 50, "from": {"id": tg.user_id, "is_bot": True}})
    elif addressing == "missing_from":
        source.pop("from")
    original = copy.deepcopy(source)
    await service.message(source)
    await chat.observe(source)
    assert await chat.run_one()
    assert source == original
    request = model.calls[0][1]
    entry = request["messages"][-1]
    assert "reply_mode" not in request and request["reply_to"] == 1
    assert entry["speaker_id"] == -1001 and entry["name"] == "匿名管理员 · 测试群"
    assert entry["bot"] is False
    assert any(method == "send_message" and data["chat_id"] == -1001
               and data["reply_parameters"]["message_id"] == 1 for method, data in tg.calls)
    async with db.sessions() as session:
        archived = await session.scalar(select(ArchivedMessage).where(ArchivedMessage.message_id == 1))
        assert archived.sender_id == -1001 and archived.sender_type == "chat"
        assert (await session.scalar(select(ChatTurn))).state == "done"
    await chat.close()


async def test_anonymous_followup_survives_rebuilt_context_and_uses_group_budget(env):
    chat, model = await configured(env)
    settings, db, tg, _ = env
    await chat.observe(anonymous(1, "@purrai_bot 周末去哪玩？"))
    assert await chat.run_one()
    await age_reply(chat)
    await chat.close()
    rebuilt_model = FakeModel()
    rebuilt = GroupChat(db, tg, settings, model=rebuilt_model)
    try:
        await rebuilt.observe(anonymous(2, "附近还有别的地方吗？"))
        assert await rebuilt.run_one()
        request = rebuilt_model.calls[0][1]
        assert "reply_mode" not in request
        people = [row for row in request["messages"] if not row["bot"]]
        assert [row["id"] for row in people] == [1, 2]
        assert {row["speaker_id"] for row in people} == {-1001}
        assert all(row["name"] == "匿名管理员 · 测试群" for row in people)
        async with db.sessions() as session:
            budget = json.loads((await session.get(Meta, "private:chat-budget")).value)
            assert budget["users"]["-1001"]["calls"] == 2
            assert "1087968824" not in budget["users"]
    finally:
        await rebuilt.close()


async def test_anonymous_image_request_keeps_unavailable_image_behavior(env):
    chat, model = await configured(env)
    source = anonymous(1, "", caption="@purrai_bot 这张图呢？", photo=[{"file_id": "photo", "file_unique_id": "unique"}])
    await chat.observe(source)
    assert await chat.run_one()
    entry = model.calls[0][1]["messages"][-1]
    assert entry["image_unavailable"] is True and entry["speaker_id"] == -1001
    await chat.close()


@pytest.mark.parametrize("text", ["@purrai_bot 来个跑分兼职，私聊我", "@other_bot /start 123", "/help"])
async def test_anonymous_messages_still_follow_advertising_and_command_filters(env, text):
    chat, model = await configured(env)
    await chat.observe(anonymous(1, text))
    assert not await chat.run_one() and model.calls == []
    await chat.close()


@pytest.mark.parametrize("changes", [
    {"sender_chat": {"id": -2001, "type": "supergroup", "title": "其他群"}},
    {"sender_chat": {"id": -1001, "type": "channel", "title": "频道"}},
    {"sender_chat": {"id": -2001, "type": "channel", "title": "其他频道"}},
    {"sender_chat": None},
    {"via_bot": {"id": 9999, "is_bot": True}},
])
async def test_anonymous_exception_does_not_allow_other_senders_or_bots(env, changes):
    chat, model = await configured(env)
    source = anonymous(1, "@purrai_bot 你好", **changes)
    assert not human_message(source)
    await chat.observe(source)
    assert not await chat.run_one() and model.calls == []
    await chat.close()


async def test_anonymous_groups_do_not_share_placeholder_identity(env):
    chat, model = await configured(env)
    _, db, _, _ = env
    async with db.transaction() as session:
        session.add(Group(id=-1002, title="另一个群", approved=True,
                          settings={**default_settings(), "chat_enabled": True}))
    await chat.observe(anonymous(1, "@purrai_bot 你好", group_id=-1001))
    await chat.observe(anonymous(2, "@purrai_bot 你好", group_id=-1002))
    assert await chat.run_one() and await chat.run_one()
    assert [call[1]["messages"][-1]["speaker_id"] for call in model.calls] == [-1001, -1002]
    async with db.sessions() as session:
        senders = set(await session.scalars(select(ChatMessage.sender_id).where(ChatMessage.is_bot.is_(False))))
        assert senders == {-1001, -1002}
    await chat.close()


def test_plain_group_anonymous_identity_does_not_invent_real_user():
    source = anonymous(1, "你好", chat={"id": -12345, "type": "group", "title": "普通群"},
                       sender_chat={"id": -12345, "type": "group", "title": "普通群"})
    assert human_message(source)
    assert chat_sender(source) == {"id": -12345, "first_name": "匿名管理员 · 普通群"}
