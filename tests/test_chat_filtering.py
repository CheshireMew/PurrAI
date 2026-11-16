"""Keep utility traffic and removed messages out of otherwise natural chat."""

import asyncio
import json

import pytest
from sqlalchemy import select

from conftest import message
from purrai.chat import GroupChat
from purrai.chat_model import ChatReply
from purrai.models import ChatMessage, ChatTurn, Group, now
from test_bot_admin import click, deliver, markup, open_group, private
from test_chat import FakeModel, age_reply, configured, due
from test_chat_context import remove


@pytest.mark.parametrize("text", ["签到", "积分", "抽奖", "注册币安", "注册 OKX", "币圈教程", "  注册 okx  ", "注册OKX"])
async def test_keyboard_labels_are_neither_reply_candidates_nor_context_even_during_followup(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，周末去哪玩"))
    await due(chat)
    assert await chat.run_one()
    await age_reply(chat)
    await chat.observe(message(2, text))
    await due(chat)
    assert not await chat.run_one()
    async with chat.db.sessions() as session:
        assert await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 2)) is None
    await chat.observe(message(3, "柴郡喵，那就去爬山吧"))
    await due(chat)
    assert await chat.run_one()
    assert 2 not in [row["id"] for row in model.calls[-1][1]["messages"]]


@pytest.mark.parametrize("text", ["柴郡喵，今天积分怎么算？", "柴郡喵，怎么辨别广告和正常分享？", "柴郡喵，为什么每次提广告你都出现？"])
async def test_normal_questions_containing_utility_or_ad_words_still_reach_model(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, text))
    await due(chat)
    assert await chat.run_one()
    assert model.calls[0][1]["messages"][0]["text"] == text
    assert "reply_mode" not in model.calls[0][1]


async def test_model_decides_whether_to_reply_to_ad_discussion(env):
    chat, model = await configured(env)
    model.result = ChatReply(should_reply=False)
    await chat.observe(message(1, "今天广告又多了啊"))
    await due(chat)
    assert await chat.run_one() and len(model.calls) == 1
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "silent" and "模型判断无需回复" in turn.error


async def test_group_ignore_setting_filters_old_frozen_messages_and_own_reply_without_reset(env):
    chat, model = await configured(env, chat_ignored_texts=[])
    await chat.observe(message(1, "币圈教程"))
    await due(chat)
    assert await chat.run_one()
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_ignored_texts": ["币圈教程"]}
    await age_reply(chat)
    await chat.observe(message(2, "柴郡喵，周末看什么电影？"))
    await due(chat)
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[-1][1]["messages"]] == [2]


async def test_deleted_frozen_source_and_cached_quote_are_filtered_after_restart_and_row_pruning(env):
    chat, model = await configured(env, chat_proactive=False)
    model.result = None
    await chat.observe(message(1, "这条内容会被清理"))
    await chat.observe(message(2, "柴郡喵，你看正常的这个问题", reply_to_message={"message_id": 1, "from": {"id": 42}}))
    await due(chat)
    assert await chat.run_one()
    # Simulate an upgraded persisted prefix containing a quote and an aged-out
    # rolling row; the durable deletion fact must still remove both references.
    async with chat.db.transaction() as session:
        from purrai.chat_state import session_state, store_state

        row, state = await session_state(session, -1001, 0)
        state["sent_context"][1]["quote"] = "这条内容会被清理"
        store_state(session, row, -1001, 0, state)
        source = await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 1))
        source.message_id = 9000
    await remove(chat, 1)
    restored_model = FakeModel()
    restored = GroupChat(chat.db, chat.tg, chat.settings, model=restored_model)
    await restored.recover()
    await restored.observe(message(3, "柴郡喵，换个正常的话题吧"))
    await due(restored)
    assert await restored.run_one()
    context = restored_model.calls[0][1]["messages"]
    assert [row["id"] for row in context] == [2, 3]
    assert "quote" not in context[0] and "reply_to" not in context[0]
    assert "这条内容会被清理" not in json.dumps(context, ensure_ascii=False)


async def test_setting_changed_during_generation_cancels_utility_reply(env):
    chat, model = await configured(env, chat_ignored_texts=[])
    await chat.observe(message(1, "币圈教程"))
    await due(chat)
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        async with chat.db.transaction() as session:
            group = await session.get(Group, -1001)
            group.settings = {**group.settings, "chat_ignored_texts": ["币圈教程"]}
    finally:
        model.release.set()
        await running
    assert len(model.calls) == 1
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)


async def test_no_artificial_wait_and_moderation_still_excludes_deleted_target(env):
    chat, model = await configured(env)
    before = now()
    await chat.observe(message(1, "柴郡喵，这条内容稍后会被删除"))
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert before <= turn.available_at <= now()
    await remove(chat, 1)
    await due(chat)
    assert not await chat.run_one() and not model.calls


async def test_separate_direct_questions_keep_separate_targets_without_artificial_wait(env):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "柴郡喵，聊聊周末吧"))
    await due(chat)
    await chat.observe(message(2, "柴郡喵，这条刚到，再等一下", user_id=44))
    await chat.observe(message(3, "先补充一条正常的文字背景", user_id=43))
    assert await chat.run_one()
    assert model.calls[0][1]["reply_to"] == 1
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [1, 3]
    async with chat.db.sessions() as session:
        second = await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 2))
        assert second.state == "pending" and second.available_at <= now()
    await age_reply(chat)
    await due(chat)
    assert await chat.run_one()
    assert model.calls[-1][1]["reply_to"] == 2
    ids = [row["id"] for row in model.calls[-1][1]["messages"]]
    assert 2 in ids and ids.count(3) == 1 and len(ids) == len(set(ids))


async def test_ignore_list_can_be_saved_and_cleared_from_telegram_panel(env):
    from purrai.runtime import Runtime

    chat, _ = await configured(env)
    settings, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await runtime.chat.close()
    runtime.chat = runtime.management.chat = chat
    await open_group(runtime, tg)
    await click(runtime, tg, "群聊天设置")
    assert "不再固定等待" in markup(tg)["text"]
    await click(runtime, tg, "聊天忽略口令")
    await deliver(runtime, {"message": private(" 查余额 \n注册 OKX\n注册okx\n签到")})
    async with db.sessions() as session:
        group = await session.get(Group, -1001)
        assert group.settings["chat_ignored_texts"] == ["查余额", "注册 OKX", "签到"]
    await click(runtime, tg, "聊天忽略口令")
    await deliver(runtime, {"message": private("-")})
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["chat_ignored_texts"] == []


async def test_ignore_list_api_validation(client, env):
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {
        "chat_ignored_texts": [" 查余额 ", "注册 OKX", "注册okx", "签到", ""],
    }})
    assert response.status_code == 200
    async with env[1].sessions() as session:
        assert (await session.get(Group, -1001)).settings["chat_ignored_texts"] == ["查余额", "注册 OKX", "签到"]
    for invalid in [["x" * 81], ["第一条\n第二条"], [str(i) for i in range(101)]]:
        response = await client.patch("/api/groups/-1001/settings", json={"settings": {"chat_ignored_texts": invalid}})
        assert response.status_code == 422
    # Per-group customization does not alter the defaults.
    from purrai.schemas import GroupSettings

    assert "查余额" not in GroupSettings().chat_ignored_texts


async def test_no_reply_list_api_blocks_all_chat_triggers_and_removal_restores_reply(client, env):
    chat, model = await configured(env)
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {
        "chat_no_reply_user_ids": [42, 42],
    }})
    assert response.status_code == 200, response.text
    async with chat.db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["chat_no_reply_user_ids"] == [42]
    samples = [
        message(1, "周末打算出去走走"),
        message(2, "@purrai_bot 你觉得呢"),
        message(3, "你再说一句", reply_to_message={"message_id": 900, "from": {"id": chat.tg.user_id, "is_bot": True}}),
        message(4, "柴郡喵，你认识我吗", **{"from": {"id": 42, "first_name": "换个昵称"}}),
    ]
    for incoming in samples:
        await chat.observe(incoming)
        await due(chat)
        assert not await chat.run_one()
    assert not model.calls
    await chat.observe(message(5, "柴郡喵，推荐周末活动吧", user_id=43))
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[-1][1]["messages"]] == [5]
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {"chat_no_reply_user_ids": []}})
    assert response.status_code == 200
    await age_reply(chat)
    await chat.observe(message(6, "柴郡喵，今天适合去公园吗"))
    assert await chat.run_one()
    assert model.calls[-1][1]["reply_to"] == 6
    for invalid in [[0], [-1], [True], [42.5], ["42"], [2**52], list(range(1, 502))]:
        response = await client.patch("/api/groups/-1001/settings", json={"settings": {"chat_no_reply_user_ids": invalid}})
        assert response.status_code == 422


async def test_no_reply_list_cancels_queued_chat_before_model_call(client, env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，明天去哪玩"))
    assert (await client.patch("/api/groups/-1001/settings", json={"settings": {"chat_no_reply_user_ids": [42]}})).status_code == 200
    await due(chat)
    assert not await chat.run_one() and not model.calls


async def test_no_reply_list_added_during_generation_stops_reply(client, env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，明天去哪玩"))
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        assert (await client.patch("/api/groups/-1001/settings", json={"settings": {"chat_no_reply_user_ids": [42]}})).status_code == 200
    finally:
        model.release.set()
        await running
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)


async def test_no_reply_list_removes_frozen_history_bot_replies_and_quoted_text(client, env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，我们来聊个只有我知道的话题"))
    assert await chat.run_one()
    assert (await client.patch("/api/groups/-1001/settings", json={"settings": {"chat_no_reply_user_ids": [42]}})).status_code == 200
    await age_reply(chat)
    await chat.observe(message(2, "柴郡喵，换个话题聊聊周末", user_id=43,
                               reply_to_message={"message_id": 1, "from": {"id": 42}, "text": "只有我知道的话题"}))
    assert await chat.run_one()
    context = model.calls[-1][1]["messages"]
    assert [row["id"] for row in context] == [2]
    assert "quote" not in context[0] and "reply_to" not in context[0]
    assert "只有我知道" not in json.dumps(context, ensure_ascii=False)


@pytest.mark.parametrize("advertisement", [False, True])
async def test_no_reply_list_preserves_keyword_ad_review_and_moderation(client, env, advertisement):
    from conftest import drain
    from test_ai_advertising_limit import save_ai_keyword

    chat, model = await configured(env, chat_no_reply_user_ids=[42])
    await save_ai_keyword(client, value="纸飞机", meaning="需结合语境判断")
    model.result = ChatReply(text="这句聊天不能发出", should_reply=True, is_advertisement=advertisement)
    incoming = message(1, "柴郡喵，这段纸飞机内容要判断")
    await env[3].message(incoming)
    await chat.observe(incoming)
    assert await chat.run_one() and len(model.calls) == 1
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).review_only
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)
    await drain(env[3])
    deleted = [data["message_id"] for method, data in chat.tg.calls if method == "delete_message"]
    assert deleted == ([1] if advertisement else [])
    if advertisement:
        assert not chat.tg.members[(-1001, 42)]["can_send_messages"]
        assert chat.tg.members[(-1001, 42)]["until_date"] == 0
    else:
        assert not any(method == "restrict_chat_member" for method, _ in chat.tg.calls)


async def test_no_reply_list_shared_setting_works_with_pre_upgrade_data(client, env):
    from purrai.models import SharedPolicy
    async with env[1].transaction() as session:
        shared = await session.get(SharedPolicy, 1)
        if shared:
            shared.settings = {key: value for key, value in shared.settings.items() if key != "chat_no_reply_user_ids"}
        group = await session.get(Group, -1001)
        group.settings = {key: value for key, value in group.settings.items() if key != "chat_no_reply_user_ids"}
    before = (await client.get("/api/settings/defaults")).json()
    assert before["settings"]["chat_no_reply_user_ids"] == []
    response = await client.patch("/api/settings/defaults", json={
        "settings": {"chat_no_reply_user_ids": [77]}, "baseline": before, "request_id": "no-reply-shared-test",
    })
    assert response.status_code == 200, response.text
    async with env[1].sessions() as session:
        assert (await session.get(Group, -1001)).settings["chat_no_reply_user_ids"] == [77]


async def test_no_reply_list_can_be_saved_and_cleared_from_telegram_panel(env):
    from purrai.runtime import Runtime
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, "群聊天设置")
    await click(runtime, tg, "不回复名单")
    await deliver(runtime, {"message": private("42\n77\n42")})
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["chat_no_reply_user_ids"] == [42, 77]
    await click(runtime, tg, "不回复名单")
    await deliver(runtime, {"message": private("-")})
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["chat_no_reply_user_ids"] == []
