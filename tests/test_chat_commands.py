"""Utility traffic is excluded before paid generation, without member punishment."""

import asyncio
import json

import pytest
from sqlalchemy import func, select

from conftest import message
from purrai import chat_commands
from purrai.chat import GroupChat
from purrai.chat_model import ChatReply
from purrai.chat_state import session_state, store_state
from purrai.models import Action, ChatMessage, ChatTurn, Group, Meta, Rule, Violation, default_settings, now
from test_chat import FakeModel, age_reply, configured, due
from test_bot_admin import click, deliver, markup, open_group, private

PRODUCTION_ADS = [
    "苹果1８只要6k", "來   回  血",
    "強得可怕，七天三十倍，勝率拉滿，狗莊高呼不可戰勝 點頭像免費來！！！",
    "來   回   血！", "超高反80% 群主福利 天天高倍爽吃 \nbtc 策略已發 👉👉",
    "新.澳.6.合.采.内.慕.进频.道了解", "④⑧⑧佺套一无限火力",
    "牛逼，有做合约的不，你们去看看这个博主 @sample_trader 胜率这么高还天天发单，妈的每天都赚个七八倍，神了",
    "0.1 TRX 闪租", "兄弟們这个社裙好准，赶快进来捡钱❗️",
    "超高反80% 群主福利 天天高倍爽吃 \nbtc 策略已發 👉👉", "來   回  血",
    "每日免費更新策略！！\n👈🏻戳竹葉速進爆啦！！", "來   回  血",
]


@pytest.mark.parametrize("text", PRODUCTION_ADS)
async def test_all_fourteen_historical_ad_calls_are_filtered_before_model(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, text))
    assert not await chat.run_one() and not model.calls
    assert not (await chat.status(-1001))["commands"]["items"]
    async with chat.db.sessions() as session:
        assert not await session.scalar(select(func.count()).select_from(Violation))
        assert not await session.scalar(select(func.count()).select_from(Action))


@pytest.mark.parametrize("text", [
    "柴郡喵，“來回血”这种话是什么意思？", "柴郡喵，苹果18只要6k是真的吗？",
    "柴郡喵，如何识别高返80%的广告？", "柴郡喵，这种社裙好准，赶快进来捡钱，是不是诈骗？",
    "柴郡喵，我准备买苹果18，预算6k，合适吗？", "柴郡喵，TRX闪租是什么原理？",
    "柴郡喵，“七天三十倍，点头像免费来”这种广告应该怎么举报？",
    "柴郡喵，我在游戏里快没血了，要怎么回血？", "柴郡喵，每日更新交易策略，该注意什么？",
])
async def test_ad_reports_quotes_and_normal_market_questions_still_reach_model(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, text))
    assert await chat.run_one() and len(model.calls) == 1


async def test_upgraded_cached_commands_and_ads_are_removed_including_associated_bot_replies(env):
    chat, model = await configured(env, chat_proactive=False)
    async with chat.db.transaction() as session:
        row, state = await session_state(session, -1001, 0)
        state.update(context_started=now(), context_reset=0, sent_context=[
            {"id": 900 + i, "speaker_id": 42, "text": text, "bot": False}
            for i, text in enumerate(["积分红包 10", "@other_bot campaign_001 g123", PRODUCTION_ADS[0]])
        ] + [{"id": 910, "speaker_id": 999, "text": "不该继续出现的旧点评", "bot": True, "reply_to": 900}])
        store_state(session, row, -1001, 0, state)
    await chat.observe(message(1, "柴郡喵，周末看什么电影？"))
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [1]


@pytest.mark.parametrize("text", [
    "积分排行", "积分红包 10", "发积分红包 1", "积分红包１０", "积分红包 10 2", "积分红包 0.5",
    "积 分红 包 10", "@other_bot campaign_001 g12345", "@other_bot start 123", "@other_bot balance",
    "@other_bot /start 123", "@other_bot 积分红包 10", "@other_bot 每日任务",
])
async def test_parameterized_and_other_bot_commands_do_not_request_model_or_enter_context(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，周末去爬山吧"))
    await chat.run_one()
    await age_reply(chat)
    await chat.observe(message(2, text))
    await due(chat)
    assert not await chat.run_one()
    assert len(model.calls) == 1
    await chat.observe(message(3, "柴郡喵，几点出发好？"))
    assert await chat.run_one()
    assert 2 not in [row["id"] for row in model.calls[-1][1]["messages"]]
    async with chat.db.sessions() as session:
        assert not await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 2))
        assert not await session.scalar(select(func.count()).select_from(Action))
        assert not await session.scalar(select(func.count()).select_from(Violation))


@pytest.mark.parametrize("text", [
    "柴郡喵，积分红包 10 是什么意思？", "柴郡喵，积分不足怎么办？", "柴郡喵，怎么发积分给别人，语法打啥",
    "@purrai_bot start 123", "柴郡喵，@other_bot campaign_001 是什么？",
    "柴郡喵，@other_bot 能不能做这个？",
])
async def test_ordinary_command_questions_and_own_bot_mentions_remain_answerable(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, text))
    assert await chat.run_one()
    assert len(model.calls) == 1


async def repeat(chat, text="每日任务", count=5, *, start=1, users=(42, 43, 44)):
    for index in range(count):
        await chat.observe(message(start + index, text, user_id=users[index % len(users)]))


async def test_counter_learns_at_threshold_and_removes_pending_and_frozen_history_after_restart(env):
    chat, _ = await configured(env, chat_proactive=False)
    await repeat(chat, count=4)
    stats = (await chat.status(-1001))["commands"]
    assert stats["items"] == [{"text": "每日任务", "count": 4, "users": 3, "ignored": False}]
    # A persisted prefix from before this feature must be filtered too.
    async with chat.db.transaction() as session:
        row, state = await session_state(session, -1001, 0)
        state.update(context_started=now(), context_reset=0, sent_context=[{
            "id": 900, "speaker_id": 42, "text": "每日任务", "bot": False,
        }])
        store_state(session, row, -1001, 0, state)
    await chat.observe(message(5, "每 日 任 务", user_id=44))
    model = FakeModel()
    restored = GroupChat(chat.db, chat.tg, chat.settings, model=model)
    await restored.recover()
    await restored.observe(message(6, "柴郡喵，帮我选个周末活动"))
    assert await restored.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [6]
    stats = (await restored.status(-1001))["commands"]
    assert stats["items"] == [{"text": "每日任务", "count": 5, "users": 3, "ignored": True}]


async def test_numeric_arguments_share_a_counter_and_are_removed_as_whole_messages(env):
    chat, model = await configured(env, chat_proactive=False)
    for index in range(5):
        await chat.observe(message(index + 1, f"领取奖励 {index + 1}", user_id=42 + index % 3))
    assert (await chat.status(-1001))["commands"]["items"] == [
        {"text": "领取奖励", "count": 5, "users": 3, "ignored": True}]
    await chat.observe(message(6, "领取奖励 99"))
    await chat.observe(message(7, "柴郡喵，领取奖励99是什么意思？"))
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [7]


async def test_one_user_redelivery_and_edits_do_not_satisfy_threshold(env):
    chat, model = await configured(env, chat_proactive=False)
    item = message(1, "每日任务")
    for _ in range(7):
        await chat.observe(item)
    assert (await chat.status(-1001))["commands"]["items"][0]["count"] == 1
    await repeat(chat, count=5, start=2, users=(42,))
    assert not (await chat.status(-1001))["commands"]["items"][0]["ignored"]
    await chat.observe(message(7, "每日任务", user_id=43), edited=True)
    await chat.observe(message(8, "每日任务", user_id=44), edited=True)
    assert (await chat.status(-1001))["commands"]["items"][0]["count"] == 6
    await chat.observe(message(1, "正常的修改", edit_date=int(now()) + 1), edited=True)
    assert (await chat.status(-1001))["commands"]["items"][0]["count"] == 5
    assert not model.calls


async def test_learning_is_group_scoped_expires_and_can_be_disabled_or_excepted(env, monkeypatch):
    chat, _ = await configured(env, chat_proactive=False)
    await repeat(chat)
    assert (await chat.status(-1001))["commands"]["items"][0]["ignored"]
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_command_exceptions": ["每日任务"]}
        session.add(Group(id=-1002, title="另一个群", approved=True,
                          settings={**default_settings(), "chat_enabled": True}))
    assert not (await chat.status(-1001))["commands"]["items"][0]["ignored"]
    assert not (await chat.status(-1002))["commands"]["items"]
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_command_exceptions": [], "chat_command_learning": False}
    assert not (await chat.status(-1001))["commands"]["items"][0]["ignored"]
    future = now() + chat_commands.WINDOW + 1
    monkeypatch.setattr(chat_commands, "now", lambda: future)
    assert not (await chat.status(-1001))["commands"]["items"]


async def test_ads_questions_bots_and_rules_never_become_learned_commands(env):
    chat, model = await configured(env, chat_proactive=False)
    async with chat.db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="积分商店", mode="exact", scope="message"))
    for index in range(6):
        for offset, text in enumerate(["來 回 血", "每日任务，看我简介赚钱", "积分商店", "每日任务怎么做？", "早上好"]):
            await chat.observe(message(index * 10 + offset + 1, text, user_id=42 + index))
        await chat.observe(message(100 + index, "每日任务", **{"from": {"id": 555, "is_bot": True}}))
    assert not (await chat.status(-1001))["commands"]["items"]
    assert not model.calls


async def test_hidden_links_forwarded_function_labels_and_stale_edits_do_not_add_evidence(env):
    chat, _ = await configured(env, chat_proactive=False)
    for index in range(5):
        await chat.observe(message(10 + index, "每日任务", user_id=42 + index, entities=[{
            "type": "text_link", "offset": 0, "length": 4, "url": "https://example.com",
        }]))
        await chat.observe(message(20 + index, "每日任务", user_id=42 + index, forward_origin={
            "type": "channel", "chat": {"id": -2002, "type": "channel"},
        }))
    assert not (await chat.status(-1001))["commands"]["items"]
    await chat.observe(message(1, "每日任务"), update_id=10)
    await chat.observe(message(1, "积分商店"), edited=True, update_id=9)
    assert (await chat.status(-1001))["commands"]["items"][0]["text"] == "每日任务"


async def test_model_ad_verdict_is_remembered_as_ad_and_not_learned_as_command(env):
    chat, model = await configured(env)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    await chat.observe(message(1, "每日奖励"))
    assert await chat.run_one()
    assert not (await chat.status(-1001))["commands"]["items"]
    await chat.observe(message(2, "每 日 奖 励", user_id=43))
    await due(chat)
    assert not await chat.run_one()
    assert len(model.calls) == 1
    # A different legitimate question must not resend the known ad in history.
    await chat.observe(message(3, "柴郡喵，周末聊点什么？"))
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[-1][1]["messages"]] == [3]


async def test_learning_during_generation_cancels_stale_reply(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "每日任务"))
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        await repeat(chat, start=2, count=4)
    finally:
        model.release.set()
        await running
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 1))
        assert turn.state == "skipped"


async def test_counter_is_bounded_and_ignores_expired_messages(env):
    chat, _ = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "每日任务", date=int(now()) - chat_commands.WINDOW - 1))
    assert not (await chat.status(-1001))["commands"]["items"]
    async with chat.db.transaction() as session:
        data = {"commands": {str(i): [[i, i, now() + i]] for i in range(300)}, "ads": {str(i): now() + i for i in range(300)}}
        chat_commands.save(session, None, -1001, data)
    async with chat.db.sessions() as session:
        row = await session.get(Meta, "chat_input:-1001")
        data = json.loads(row.value)
        assert len(data["commands"]) == len(data["ads"]) == chat_commands.MAX_COMMANDS


async def test_counter_and_learning_controls_are_available_in_telegram_panel(env):
    from purrai.runtime import Runtime

    chat, _ = await configured(env, chat_proactive=False)
    await repeat(chat)
    settings, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await runtime.chat.close()
    runtime.chat = runtime.management.chat = chat
    await open_group(runtime, tg)
    await click(runtime, tg, "群聊天设置")
    assert "每日任务 5次/3人（已忽略）" in markup(tg)["text"]
    await click(runtime, tg, "口令识别次数")
    await deliver(runtime, {"message": private("7")})
    assert (await chat.status(-1001))["commands"]["threshold"] == 7
    await click(runtime, tg, "口令识别例外")
    await deliver(runtime, {"message": private("每日任务")})
    await click(runtime, tg, "已开启 · 自动识别口令")
    async with db.sessions() as session:
        cfg = (await session.get(Group, -1001)).settings
        assert cfg["chat_command_exceptions"] == ["每日任务"]
        assert not cfg["chat_command_learning"]


async def test_counter_api_and_settings_validation(client, env):
    chat, _ = await configured(env, chat_proactive=False)
    await repeat(chat)
    response = await client.get("/api/groups/-1001/chat")
    assert response.status_code == 200
    assert response.json()["commands"]["items"][0]["ignored"]
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {
        "chat_command_threshold": 8, "chat_command_learning": False,
        "chat_command_exceptions": [" 每日任务 ", "每日任务"],
    }})
    assert response.status_code == 200
    response = await client.get("/api/groups/-1001/chat")
    assert not response.json()["commands"]["enabled"]
    for options in [{"chat_command_threshold": 2}, {"chat_command_threshold": 51},
                    {"chat_command_exceptions": ["x" * 81]}]:
        response = await client.patch("/api/groups/-1001/settings", json={"settings": options})
        assert response.status_code == 422
