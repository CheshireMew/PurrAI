import asyncio
import json

import pytest
from aiogram.types import Message
from sqlalchemy import select

from conftest import drain
from purrai.chat_model import ChatModelError, ChatReply
from purrai.models import AdvertisementReview, ChatTurn, Group, Violation
from purrai.runtime import Runtime
from test_advertisement_review import records, reviewer
from test_bio_advertising import profiles
from test_chat import FakeModel
from test_points_bot import configure, flush, user_message
from test_points_trust import contribution


async def combined(env, *, chat=True):
    worker, audit = await reviewer(env, lambda _: False)
    async with env[1].transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_enabled": chat, "chat_proactive": False, "ai_ad_limit": 1}
    runtime = Runtime(env[1], env[2], env[3])
    model = FakeModel()
    runtime.chat.model = model
    return runtime, worker, model, audit


async def test_whitelisted_member_can_chat_without_advertising_review(env):
    from purrai.models import Whitelist
    runtime, worker, model, audit = await combined(env)
    async with env[1].transaction() as session:
        session.add(Whitelist(group_id=-1001, user_id=42, verification=True, moderation=True))
    try:
        await runtime.events.message(Message.model_validate(user_message(160, "柴郡喵，今天收入很不错")))
        assert await runtime.chat.run_one()
        assert len(model.calls) == 1 and not audit.calls
        assert "目标已放行。本次只判断是否接话" in model.calls[0][0]
        assert not await worker.run_one()
        assert not await records(env[1], Violation)
    finally:
        await runtime.stop()


@pytest.mark.parametrize("advertisement", [False, True])
async def test_one_request_receives_name_bio_body_and_returns_verdict_and_optional_reply(env, client, advertisement):
    await configure(client)
    runtime, worker, model, audit = await combined(env)
    profiles(env[2], {42: "青岚软件咨询 https://example.test"})
    text = "柴郡喵，青岚软件能整理数据并自动生成表格"
    model.result = ChatReply("测试回复", is_advertisement=advertisement, decision_reason="判断正文是否在宣传产品")
    incoming = user_message(101, text)
    incoming["from"]["first_name"] = "青岚软件"
    try:
        await runtime.events.message(Message.model_validate(incoming))
        review, = await records(env[1], AdvertisementReview)
        assert review.message.get("_chat_turn_id")
        assert not await worker.run_one() and not audit.calls
        assert await runtime.chat.run_one()
        assert len(model.calls) == 1 and not audit.calls
        target = next(row for row in model.calls[0][1]["messages"] if row["id"] == 101)
        assert target["name"] == "青岚软件" and target["bio"].startswith("青岚软件咨询") and target["text"] == text.replace("，", ",")
        review, = await records(env[1], AdvertisementReview)
        assert review.state == ("advertisement" if advertisement else "clean")
        await flush(env[3], env[1])
        replies = [data for method, data in env[2].calls if method == "send_message"]
        assert bool(replies) == (not advertisement)
        assert bool(await records(env[1], Violation)) == advertisement
        assert not await worker.run_one() and not audit.calls
    finally:
        await runtime.stop()


async def test_high_contribution_uses_one_pure_chat_request_without_advertising_prompt(env, client):
    await configure(client)
    await contribution(env, 500)
    runtime, worker, model, audit = await combined(env)
    try:
        await runtime.events.message(Message.model_validate(user_message(102, "柴郡喵，我们聊聊这个合作项目")))
        assert await runtime.chat.run_one()
        assert len(model.calls) == 1 and not audit.calls
        assert "本次只判断是否接话" in model.calls[0][0]
        assert "keyword_hints" not in model.calls[0][1]
        assert not await worker.run_one()
        review, = await records(env[1], AdvertisementReview)
        assert review.state == "exempt" and review.result["source"] == "points"
    finally:
        await runtime.stop()


@pytest.mark.parametrize("chat, text", [(False, "今天聊点什么"), (True, "没有叫到机器人的普通讨论")])
async def test_messages_without_chat_request_keep_independent_auditing(env, chat, text):
    runtime, worker, model, audit = await combined(env, chat=chat)
    try:
        await runtime.events.message(Message.model_validate(user_message(103, text)))
        assert await worker.run_one()
        assert len(audit.calls) == 1 and not model.calls
        assert not await runtime.chat.run_one()
    finally:
        await runtime.stop()


async def test_combined_delivery_retry_reuses_the_original_model_response(env):
    runtime, worker, model, audit = await combined(env)
    env[2].fail["send_message"] = 1
    try:
        await runtime.events.message(Message.model_validate(user_message(104, "柴郡喵，我们下周一起出去玩吧")))
        assert await runtime.chat.run_one()
        async with env[1].transaction() as session:
            turn = await session.scalar(select(ChatTurn))
            assert json.loads(turn.response)["combined_review"]
            turn.state, turn.available_at = "pending", 0
            from purrai.chat_state import session_state, store_state
            row, state = await session_state(session, -1001, 0)
            state["blocked_until"] = 0
            store_state(session, row, -1001, 0, state)
        assert await runtime.chat.run_one()
        assert len(model.calls) == 1 and not audit.calls
    finally:
        await runtime.stop()


async def test_chat_failure_returns_unfinished_verdict_to_independent_queue(env):
    runtime, worker, model, audit = await combined(env)
    model.result = ChatModelError("模拟模型错误", retryable=False)
    try:
        await runtime.events.message(Message.model_validate(user_message(105, "柴郡喵，今天计划是什么")))
        assert await runtime.chat.run_one()
        assert await worker.run_one()
        assert len(audit.calls) == len(model.calls) == 1
        review, = await records(env[1], AdvertisementReview)
        assert review.state == "clean"
    finally:
        await runtime.stop()


async def test_independent_worker_cannot_claim_message_before_chat_routing_finishes(env, monkeypatch):
    runtime, worker, model, audit = await combined(env)
    original = runtime.chat.observe

    async def while_routing(*args, **kwargs):
        assert not await worker.run_one()
        assert not audit.calls
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.chat, "observe", while_routing)
    try:
        await runtime.events.message(Message.model_validate(user_message(107, "柴郡喵，今天一起看看这个话题")))
        assert await runtime.chat.run_one()
        assert len(model.calls) == 1 and not audit.calls
    finally:
        await runtime.stop()


async def test_crash_before_chat_routing_finishes_restores_independent_review(env):
    from purrai.advertisement_routing import recover_routing
    from purrai.models import now
    worker, audit = await reviewer(env, lambda _: False)
    await env[3].message(user_message(108, "这条用于模拟事件处理中断"), chat_routing=True)
    assert not await worker.run_one()
    async with env[1].transaction() as session:
        await recover_routing(session, startup_before=now())
    assert await worker.run_one()
    assert len(audit.calls) == 1


async def test_edit_during_combined_request_does_not_punish_or_reply_to_old_content(env):
    runtime, worker, model, audit = await combined(env)
    model.result = ChatReply(is_advertisement=True)
    model.release = asyncio.Event()
    incoming = user_message(106, "柴郡喵，测试旧版本内容")
    try:
        await runtime.events.message(Message.model_validate(incoming))
        task = asyncio.create_task(runtime.chat.run_one())
        await asyncio.wait_for(model.started.wait(), 5)
        await env[3].message({**incoming, "text": "现在已经改成普通内容", "edit_date": incoming["date"] + 1}, edited=True, update_id=2)
        model.release.set()
        await task
        await drain(env[3])
        assert not await records(env[1], Violation)
        assert not any(method == "send_message" for method, _ in env[2].calls)
    finally:
        model.release.set()
        await runtime.stop()
