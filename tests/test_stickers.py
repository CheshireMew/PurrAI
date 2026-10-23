import asyncio
import json
from datetime import datetime

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendSticker
from pydantic import ValidationError
from sqlalchemy import func, select

from conftest import message
from purrai.chat_model import ChatModel, ChatModelError, ChatReply
from purrai.config import Settings
from purrai.models import ArchivedMessage, ChatMessage, ChatTurn, Group, Meta, Violation, now
from purrai.schemas import GroupSettings
from purrai.stickers import DEFAULT_CHAT_PACKS, emoji_key, pack_name, sticker_emojis
from test_bot_admin import click, deliver, markup, open_group, private
from test_chat import age_reply, configured, due


def item(identifier, emoji="😸", name="myadestes"):
    return {"file_id": f"file-{identifier}", "file_unique_id": str(identifier), "emoji": emoji, "set_name": name}


async def cache(chat, name="myadestes", items=None, **extra):
    value = {"name": name, "stickers": items or [item(1)], "retry_at": now() + 86400, "error": "", **extra}
    async with chat.db.transaction() as session:
        row = await session.get(Meta, chat.stickers.cache_key(name))
        if row:
            row.value = json.dumps(value)
        else:
            session.add(Meta(key=chat.stickers.cache_key(name), value=json.dumps(value)))


async def state(chat, **changes):
    async with chat.db.transaction() as session:
        row, value = await chat.stickers.state(session, -1001)
        value.update(changes)
        chat.stickers.save(session, row, -1001, value)


async def setup_reminder(env, monkeypatch, **options):
    chat, model = await configured(env, **options)

    class Day:
        @classmethod
        def now(cls, tz):
            return datetime(2026, 10, 20, 12, tzinfo=tz)

    monkeypatch.setattr("purrai.stickers.datetime", Day)
    await cache(chat, "FTX888", [item(11, "⚠", "FTX888"), item(12, "⚠", "FTX888")])
    await state(chat, reminder_next=now() - 1, messages=50, last_activity=now(), topic_id=77)
    return chat, model


def test_defaults_and_link_validation():
    cfg = GroupSettings(chat_sticker_packs=["https://t.me/addstickers/myadestes", "MYADESTES"])
    assert cfg.chat_sticker_packs == ["myadestes"]
    assert len(DEFAULT_CHAT_PACKS) == 20 and cfg.chat_sticker_interval == 60
    assert cfg.chat_antifraud_packs == ["FTX888"] and cfg.chat_antifraud_interval == 21600
    assert pack_name("t.me/addstickers/HMS_Cheshire") == "HMS_Cheshire"
    with pytest.raises(ValidationError):
        GroupSettings(chat_sticker_packs=["https://t.me/addemoji/not-a-sticker"])


async def test_chat_sends_text_then_sticker_and_keeps_both_in_context(env):
    chat, model = await configured(env, chat_proactive=False)
    model.result = ChatReply("周末出去玩吧！", "😸")
    await cache(chat)
    await chat.observe(message(1, "柴郡喵好开心呀", message_thread_id=77, is_topic_message=True))
    await due(chat)
    assert await chat.run_one()
    sent = [data for method, data in chat.tg.calls if method == "send_sticker"]
    assert len(sent) == 1 and sent[0]["sticker"] == "file-1"
    assert sent[0]["message_thread_id"] == 77 and sent[0]["reply_parameters"]["message_id"] == 1
    outgoing = [(method, data) for method, data in chat.tg.calls if method in ("send_message", "send_sticker")]
    assert [method for method, _ in outgoing] == ["send_message", "send_sticker"]
    assert outgoing[0][1]["text"] == "周末出去玩吧！"
    assert len(model.calls) == 1 and model.calls[0][1]["sticker_emojis"] == ["😸"]
    assert "file-1" not in model.payloads[0] and "myadestes" not in model.payloads[0]
    async with chat.db.sessions() as session:
        own = (await session.scalars(select(ChatMessage).where(ChatMessage.is_bot).order_by(ChatMessage.id))).all()
        assert [row.text for row in own] == ["周末出去玩吧！", "[贴纸 😸]"]
        archived = (await session.scalars(select(ArchivedMessage).order_by(ArchivedMessage.message_id))).all()
        assert [row.message_type for row in archived] == ["text", "sticker"]
    await state(chat, last_sticker=now() - 70)
    await cache(chat, items=[item(1), item(2, "🐱")])
    model.result = ChatReply("喵", "🐱")
    async with chat.db.transaction() as session:
        row = await session.get(Meta, "chat_session:-1001:77")
        value = json.loads(row.value)
        value.update(last_reply=now() - 10, last_attempt=now() - 10)
        row.value = json.dumps(value)
    await chat.observe(message(2, "柴郡喵你也卖个萌", message_thread_id=77, is_topic_message=True))
    await due(chat)
    assert await chat.run_one()
    assert [data["sticker"] for method, data in chat.tg.calls if method == "send_sticker"] == ["file-1", "file-2"]
    prefix = model.payloads[0].split('],"reply_to":', 1)[0]
    assert model.payloads[1].startswith(prefix)
    assert any(row["bot"] and "贴纸" in row["text"] for row in model.calls[-1][1]["messages"])


@pytest.mark.parametrize("options", [
    {"chat_stickers": False}, {"chat_sticker_packs": []}, {},
])
async def test_disabled_missing_or_unavailable_stickers_fall_back_to_text(env, options):
    chat, model = await configured(env, **options)
    model.result = ChatReply("开心呀", "😸")
    await chat.observe(message(1, "柴郡喵今天好开心"))
    await due(chat)
    await chat.run_one()
    sent = [data for method, data in chat.tg.calls if method == "send_message"]
    assert len(sent) == 1 and sent[0]["text"] == "开心呀"
    assert not any(method == "send_sticker" for method, _ in chat.tg.calls)


async def test_stickers_have_group_wide_cooldown_across_topics(env):
    chat, model = await configured(env)
    await cache(chat, items=[item(1), item(2)])
    model.result = ChatReply("先文字接话", "😸")
    await chat.observe(message(1, "柴郡喵你好", message_thread_id=77, is_topic_message=True))
    await chat.observe(message(2, "柴郡喵这里也在聊", message_thread_id=88, is_topic_message=True))
    await due(chat)
    await asyncio.gather(chat.run_one(), chat.run_one())
    assert len([1 for method, _ in chat.tg.calls if method == "send_sticker"]) == 1
    assert len([1 for method, _ in chat.tg.calls if method == "send_message"]) == 2


async def test_sticker_rate_limit_reuses_same_file_and_model_reply_after_restart(env):
    from purrai.chat import GroupChat

    chat, model = await configured(env)
    await cache(chat, items=[item(1), item(2)])
    model.result = ChatReply(None, "😸")
    original, attempts = chat.tg.call, []

    async def limited(method, **kwargs):
        if method == "send_sticker":
            attempts.append(kwargs["sticker"])
            if len(attempts) == 1:
                raise TelegramRetryAfter(method=SendSticker(chat_id=-1001, sticker="file-1"),
                                         message="wait", retry_after=1)
        return await original(method, **kwargs)

    chat.tg.call = limited
    await chat.observe(message(1, "柴郡喵你好呀"))
    await due(chat)
    await chat.run_one()
    restored = GroupChat(chat.db, chat.tg, chat.settings, model=model)
    await restored.recover()
    await due(restored)
    await restored.run_one()
    assert len(model.calls) == 1 and len(attempts) == 2 and attempts[0] == attempts[1]


async def test_telegram_rejects_sticker_falls_back_without_new_model_request(env):
    chat, model = await configured(env)
    await cache(chat)
    model.result = ChatReply("文字也能接话", "😸")
    original = chat.tg.call

    async def rejected(method, **kwargs):
        if method == "send_sticker":
            raise TelegramBadRequest(method=SendSticker(chat_id=-1001, sticker="file-1"), message="forbidden sticker")
        return await original(method, **kwargs)

    chat.tg.call = rejected
    await chat.observe(message(1, "柴郡喵你好呀"))
    await due(chat)
    await chat.run_one()
    assert len(model.calls) == 1
    assert [data["text"] for method, data in chat.tg.calls if method == "send_message"] == ["文字也能接话"]
    assert (await chat.status(-1001))["stickers"]["error"]


async def test_unknown_send_result_is_not_retried(env):
    chat, model = await configured(env)
    await cache(chat)
    chat.tg.fail["send_sticker"] = 1
    model.result = ChatReply("备用文字", "😸")
    await chat.observe(message(1, "柴郡喵你好呀"))
    await due(chat)
    await chat.run_one()
    await chat.recover()
    assert not await chat.run_one()
    assert [data["text"] for method, data in chat.tg.calls if method == "send_message"] == ["备用文字"]
    async with chat.db.sessions() as session:
        assert await session.scalar(select(ChatTurn.state)) == "failed"


async def test_removed_target_during_sticker_selection_is_never_sent(env):
    chat, model = await configured(env)
    model.result = ChatReply("不能发送", "😸")
    original = chat.stickers.choose

    async def choose(*args):
        async with chat.db.transaction() as session:
            session.add(Violation(group_id=-1001, message_id=1, user_id=42, reasons=["广告"]))
        return await original(*args)

    chat.stickers.choose = choose
    await cache(chat)
    await chat.observe(message(1, "柴郡喵你好呀"))
    await due(chat)
    await chat.run_one()
    assert not any(method in ("send_sticker", "send_message") for method, _ in chat.tg.calls)


async def test_human_sticker_reply_is_clean_short_input_bots_and_violations_excluded(env):
    chat, model = await configured(env, chat_proactive=False)
    reply = {"message_id": 900, "from": {"id": chat.tg.user_id, "is_bot": True}}
    await chat.observe(message(1, "", sticker=item(1, "🥰"), reply_to_message=reply))
    await chat.observe(message(2, "", sticker=item(2), **{"from": {"id": 77, "is_bot": True}}))
    async with chat.db.transaction() as session:
        session.add(Violation(group_id=-1001, message_id=3, user_id=42, reasons=["被删贴纸"]))
    await chat.observe(message(3, "", sticker=item(3)))
    await due(chat)
    await chat.run_one()
    assert [row["text"] for row in model.calls[0][1]["messages"]] == ["[贴纸 🥰]"]


async def test_refresh_cache_backoff_partial_failure_and_restart(env):
    chat, _ = await configured(env)
    original = chat.tg.call

    async def packs(method, **kwargs):
        if method == "get_sticker_set":
            chat.tg.calls.append((method, kwargs))
            if kwargs["name"] == "bad":
                raise ConnectionError("offline")
            return {"name": kwargs["name"], "sticker_type": "regular", "stickers": [item(1)]}
        return await original(method, **kwargs)

    chat.tg.call = packs
    await chat.stickers.refresh(["myadestes", "bad"])
    await chat.stickers.refresh(["myadestes", "bad"])
    assert len([1 for method, _ in chat.tg.calls if method == "get_sticker_set"]) == 2
    assert await chat.stickers.choose(["myadestes", "bad"], "😸")
    status = await chat.stickers.status(GroupSettings(chat_sticker_packs=["myadestes", "bad"]), -1001)
    assert status["ready"] == 1 and status["unavailable"] == ["bad"]


async def test_tags_match_requested_emoji_without_fixed_emotions_or_repeats(env):
    chat, _ = await configured(env)
    await cache(chat, items=[item(1, "🐱"), item(2, "😜"), item(3, "😭")])
    assert await chat.stickers.choose(["myadestes"], "🐱", recent=["1"]) is None
    assert (await chat.stickers.choose(["myadestes"], "😜"))["file_unique_id"] == "2"
    assert await chat.stickers.choose(["myadestes"], "😡") is None


async def test_all_library_emojis_are_available_and_new_tags_appear_automatically(env):
    chat, _ = await configured(env)
    emojis = ["☕️", "🚬", "💻", "💰", "🫪", "👩‍❤️‍👩", "🏳️‍🌈", "👨🏿‍🎓", "🎉", "😌", "🙂‍↔️"]
    await cache(chat, items=[item(i, emoji) for i, emoji in enumerate(emojis)])
    cfg = GroupSettings()
    async with chat.db.sessions() as session:
        options = await chat.stickers.options(session, -1001, cfg)
    assert set(options) == {emoji_key(e) for e in emojis}
    for i, emoji in enumerate(emojis):
        chosen = await chat.stickers.choose(cfg.chat_sticker_packs, emoji)
        assert chosen["file_unique_id"] == str(i)
    # Adding a new tag requires no classification or source-code change.
    await cache(chat, items=[item("new", "🦕")])
    async with chat.db.sessions() as session:
        assert await chat.stickers.options(session, -1001, cfg) == ["🦕"]


async def test_presentation_variants_merge_without_splitting_joined_emojis(env):
    chat, _ = await configured(env)
    await cache(chat, items=[item(1, "❤"), item(2, "❤️"), item(3, "👩‍❤️‍👩"), item(4, "👨🏿‍🎓")])
    packs = await chat.stickers.catalog(["myadestes"])
    assert len(sticker_emojis(packs)) == 3
    assert emoji_key("❤") == emoji_key("❤️")
    assert await chat.stickers.choose(["myadestes"], "👩") is None
    assert (await chat.stickers.choose(["myadestes"], "👩‍❤‍👩"))["file_unique_id"] == "3"
    assert await chat.stickers.choose(["myadestes"], "👨‍🎓") is None
    assert (await chat.stickers.status(GroupSettings(), -1001))["emoji_count"] == 3


async def test_chat_uses_sticker_tag_outside_old_emotion_categories(env):
    chat, model = await configured(env)
    await cache(chat, items=[item(1, "☕️")])
    model.result = ChatReply("一起喝杯咖啡吧 ☕", "☕")
    await chat.observe(message(1, "柴郡喵来杯咖啡"))
    await due(chat)
    await chat.run_one()
    assert model.calls[0][1]["sticker_emojis"] == ["☕"]
    assert "sticker_moods" not in model.calls[0][1]
    assert [data["sticker"] for method, data in chat.tg.calls if method == "send_sticker"] == ["file-1"]


async def test_inline_emoji_is_removed_when_no_matching_sticker_is_available(env):
    chat, model = await configured(env)
    text = "喝杯咖啡歇歇 ☕😌🙊👨🏿‍🎓🎉🫪"
    model.result = ChatReply(text)
    await chat.observe(message(1, "柴郡喵休息一下"))
    await due(chat)
    await chat.run_one()
    assert [data["text"] for method, data in chat.tg.calls if method == "send_message"] == ["喝杯咖啡歇歇"]
    assert not any(method == "send_sticker" for method, _ in chat.tg.calls)


async def test_active_group_reminder_sends_once_without_model_or_chat_context(env, monkeypatch):
    chat, model = await setup_reminder(env, monkeypatch)
    assert await chat.stickers.reminder(-1001)
    assert not await chat.stickers.reminder(-1001)
    assert model.calls == []
    sent = [data for method, data in chat.tg.calls if method == "send_sticker"]
    assert len(sent) == 1 and sent[0]["message_thread_id"] == 77
    async with chat.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 0
        assert await session.scalar(select(func.count()).select_from(ArchivedMessage)) == 1
        _, value = await chat.stickers.state(session, -1001)
        assert value["messages"] == 0 and value["reminder_state"] == "done"


@pytest.mark.parametrize("changes,options", [
    ({"messages": 49}, {}), ({"last_activity": 1}, {}), ({"reminder_next": 9e15}, {}),
    ({"last_sticker": 9e15}, {}), ({}, {"chat_enabled": False}), ({}, {"chat_antifraud": False}),
    ({}, {"chat_antifraud_packs": []}),
])
async def test_reminder_respects_activity_cooldown_and_switches(env, monkeypatch, changes, options):
    chat, _ = await setup_reminder(env, monkeypatch, **options)
    await state(chat, **changes)
    assert not await chat.stickers.reminder(-1001)
    assert not any(method == "send_sticker" for method, _ in chat.tg.calls)


async def test_reminder_night_and_ambiguous_failure_are_not_retried(env, monkeypatch):
    chat, _ = await setup_reminder(env, monkeypatch)

    class Night:
        @classmethod
        def now(cls, tz):
            return datetime(2026, 10, 20, 23, tzinfo=tz)

    from purrai import stickers
    day = stickers.datetime
    monkeypatch.setattr(stickers, "datetime", Night)
    assert not await chat.stickers.reminder(-1001)
    monkeypatch.setattr(stickers, "datetime", day)
    chat.tg.fail["send_sticker"] = 1
    assert not await chat.stickers.reminder(-1001)
    assert not await chat.stickers.reminder(-1001)
    assert len([1 for method, _ in chat.tg.calls if method == "send_sticker"]) == 1


async def test_reminder_rate_limit_defers_and_next_reminder_avoids_previous_file(env, monkeypatch):
    chat, _ = await setup_reminder(env, monkeypatch)
    original, first = chat.tg.call, True

    async def limited(method, **kwargs):
        nonlocal first
        if method == "send_sticker" and first:
            first = False
            raise TelegramRetryAfter(method=SendSticker(chat_id=-1001, sticker="file-11"),
                                     message="wait", retry_after=70)
        return await original(method, **kwargs)

    chat.tg.call = limited
    assert not await chat.stickers.reminder(-1001)
    assert not await chat.stickers.reminder(-1001)
    await state(chat, reminder_next=now() - 1)
    assert await chat.stickers.reminder(-1001)
    await state(chat, reminder_next=now() - 1, reminder_attempted=now() - 21601,
                messages=50, last_sticker=now() - 400)
    assert await chat.stickers.reminder(-1001)
    sent = [data["sticker"] for method, data in chat.tg.calls if method == "send_sticker"]
    assert len(sent) == 2 and sent[0] != sent[1]


async def test_reminder_rechecks_switch_after_async_selection(env, monkeypatch):
    chat, _ = await setup_reminder(env, monkeypatch)
    original = chat.stickers.choose

    async def choose(*args, **kwargs):
        async with chat.db.transaction() as session:
            group = await session.get(Group, -1001)
            group.settings = {**group.settings, "chat_antifraud": False}
        return await original(*args, **kwargs)

    chat.stickers.choose = choose
    assert not await chat.stickers.reminder(-1001)
    assert not any(method == "send_sticker" for method, _ in chat.tg.calls)


async def test_changed_reminder_interval_is_used_immediately(env, monkeypatch):
    chat, _ = await setup_reminder(env, monkeypatch)
    await state(chat, reminder_started=now() - 7200)
    # The original six-hour interval has not elapsed. Changing it to one hour takes effect.
    assert not await chat.stickers.reminder(-1001)
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_antifraud_interval": 3600}
    assert await chat.stickers.reminder(-1001)
    await state(chat, reminder_attempted=now() - 7200, messages=50, last_sticker=now() - 100)
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_antifraud_interval": 43200}
    assert not await chat.stickers.reminder(-1001)


async def test_disabling_stickers_during_rate_limit_uses_saved_fallback(env):
    chat, model = await configured(env)
    await cache(chat)
    model.result = ChatReply("这次先用文字", "😸")
    original = chat.tg.call

    async def limited(method, **kwargs):
        if method == "send_sticker":
            raise TelegramRetryAfter(method=SendSticker(chat_id=-1001, sticker="file-1"),
                                     message="wait", retry_after=1)
        return await original(method, **kwargs)

    chat.tg.call = limited
    await chat.observe(message(1, "柴郡喵你好呀"))
    await due(chat)
    await chat.run_one()
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_stickers": False}
    await age_reply(chat)
    await due(chat)
    await chat.run_one()
    assert len(model.calls) == 1
    assert [data["text"] for method, data in chat.tg.calls if method == "send_message"] == ["这次先用文字"]


@pytest.mark.parametrize("sticker,valid", [
    ("🐱", True), ("☕️", True), ("👨🏿‍🎓", True), ("🫪", True), (None, True),
    ("", False), ("x" * 65, False), (123, False),
])
async def test_model_sticker_contract(sticker, valid):
    model = ChatModel(Settings(_env_file=None, chat_api_key="offline"), httpx.MockTransport(lambda _: httpx.Response(
        200, json={"choices": [{"message": {"content": json.dumps({"is_advertisement": False, "should_reply": True, "reply": None, "sticker": sticker})}}]},
    )))
    try:
        if valid:
            assert await model.reply("system", "context") == ChatReply(None, sticker)
        else:
            with pytest.raises(ChatModelError):
                await model.reply("system", "context")
    finally:
        await model.close()


async def test_bot_and_web_sticker_controls(env, client):
    from purrai.runtime import Runtime

    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, "群聊天设置")
    assert "聊天贴纸" in markup(tg)["text"]
    await click(runtime, tg, "已开启 · 聊天使用贴纸")
    await click(runtime, tg, "聊天贴纸包")
    await deliver(runtime, {"message": private("https://t.me/addstickers/myadestes\nHMS_Cheshire")})
    async with db.sessions() as session:
        cfg = GroupSettings.model_validate((await session.get(Group, -1001)).settings)
    assert not cfg.chat_stickers and cfg.chat_sticker_packs == ["myadestes", "HMS_Cheshire"]
    cfg.chat_sticker_interval = 30
    response = await client.patch("/api/groups/-1001/settings", json={"settings": cfg.model_dump()})
    assert response.status_code == 200 and response.json()["settings"]["chat_sticker_interval"] == 30
    status = (await client.get("/api/groups/-1001/chat")).json()
    assert "stickers" in status and "file_id" not in json.dumps(status)
