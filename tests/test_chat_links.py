import asyncio
import json

import pytest
from sqlalchemy import select

from conftest import message
from purrai.archive import capture
from purrai.chat_model import ChatReply
from purrai.models import Action, ChatMessage, ChatTurn, Meta, Rule, Violation, now
from test_chat import configured, due


LINK = "https://x.com/crypto_maxray/status/211695578338578180?x=46"


@pytest.mark.parametrize("text,extra", [
    (LINK, {}),
    ("@purrai_bot 帮我看看 " + LINK, {}),
    ("柴郡喵，这个怎么看？www.example.com/post", {}),
    ("柴郡喵看看 example.com", {}),
    ("https://exa\u200bmple.com/post", {}),
    ("ｈｔｔｐｓ：／／ｅｘａｍｐｌｅ．ｃｏｍ", {}),
    ("tg://resolve?domain=example", {}),
    ("柴郡喵，点这里", {"entities": [{"type": "text_link", "offset": 4, "length": 3, "url": LINK}]}),
    ("", {"caption": LINK, "photo": [{"file_id": "image"}]}),
    ("", {"caption": "柴郡喵看看", "caption_entities": [{"type": "text_link", "offset": 0, "length": 5, "url": LINK}],
          "document": {"file_id": "doc"}}),
])
async def test_link_messages_never_reach_model_or_send_text_or_stickers(env, text, extra):
    chat, model = await configured(env)
    await chat.observe(message(1, text, **extra))
    await due(chat)
    assert not await chat.run_one()
    assert not model.calls
    assert not any(method in ("send_message", "send_sticker", "send_chat_action") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        assert await session.scalar(select(ChatTurn)) is None
        assert await session.scalar(select(Violation)) is None
        assert await session.scalar(select(Action)) is None


@pytest.mark.parametrize("hidden", [False, True])
async def test_replies_to_links_are_silent_even_when_addressed(env, hidden):
    chat, model = await configured(env)
    source = message(700, "看这里" if hidden else LINK)
    if hidden:
        source["entities"] = [{"type": "text_link", "offset": 0, "length": 3, "url": LINK}]
    await chat.observe(message(1, "柴郡喵，这个怎么看？", reply_to_message=source))
    assert not await chat.run_one() and not model.calls


async def test_allowed_link_is_still_ignored_and_plain_chat_can_continue(env):
    chat, model = await configured(env)
    async with chat.db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="allow_domain", value="x.com"))
    await chat.observe(message(1, LINK))
    await chat.observe(message(2, "柴郡喵，晚饭吃什么比较好？"))
    await due(chat)
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [2]


@pytest.mark.parametrize("hidden", [False, True])
async def test_legacy_link_tasks_are_skipped_before_typing_or_model(env, hidden):
    chat, model = await configured(env)
    text = "柴郡喵看这里" if hidden else LINK
    incoming = message(1, text)
    if hidden:
        incoming["entities"] = [{"type": "text_link", "offset": 0, "length": 6, "url": LINK}]
    async with chat.db.transaction() as session:
        await capture(session, incoming)
        session.add(ChatMessage(group_id=-1001, message_id=1, sender_id=42, sender_name="成员", text=text,
                                eligible=True, sent_at=now()))
        session.add(ChatTurn(group_id=-1001, message_id=1, direct=True))
    await due(chat)
    assert not await chat.run_one() and not model.calls
    assert not any(method in ("send_chat_action", "send_message", "send_sticker") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        assert await session.scalar(select(ChatTurn.state)) == "skipped"


@pytest.mark.parametrize("hidden", [False, True])
async def test_frozen_link_history_and_old_bot_comment_are_excluded(env, hidden):
    chat, model = await configured(env)
    text = "看这里" if hidden else LINK
    incoming = message(1, text)
    if hidden:
        incoming["entities"] = [{"type": "text_link", "offset": 0, "length": 3, "url": LINK}]
    context = [
        {"id": 1, "speaker_id": 42, "name": "成员", "bot": False, "text": text},
        {"id": 2, "speaker_id": 999, "name": "机器人", "bot": True, "text": "链接我打不开啦", "reply_to": 1},
    ]
    async with chat.db.transaction() as session:
        await capture(session, incoming)
        session.add(Meta(key="chat_session:-1001:0", value=json.dumps({
            "context_started": now(), "context_reset": 0, "sent_context": context, "context_cursor": 0,
        })))
    await chat.observe(message(3, "柴郡喵，换个话题聊晚饭吧"))
    await due(chat)
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [3]
    assert "链接我打不开" not in model.payloads[0] and "x.com" not in model.payloads[0]


@pytest.mark.parametrize("hidden", [False, True])
async def test_link_added_during_generation_cancels_text_and_sticker(env, hidden):
    chat, model = await configured(env)
    model.result = ChatReply("这条回复不应发送", "😄")
    text = "柴郡喵，帮我看看这个"
    await chat.observe(message(1, text))
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        incoming = message(1, text if hidden else text + LINK, edit_date=int(now()) + 1)
        if hidden:
            incoming["entities"] = [{"type": "text_link", "offset": 0, "length": 3, "url": LINK}]
        await chat.observe(incoming, edited=True, update_id=2)
    finally:
        model.release.set()
        await running
    assert len(model.calls) == 1
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        row = await session.scalar(select(ChatMessage))
        assert not row.eligible
        assert await session.scalar(select(ChatTurn.state)) == "skipped"
