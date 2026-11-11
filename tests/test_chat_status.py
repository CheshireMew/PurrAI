"""Operational status follows real failures rather than informational turn reasons."""

import json

import pytest
from sqlalchemy import select

from conftest import message
from purrai.bot_admin import BotAdmin
from purrai.chat_model import ChatModelError, ChatReply
from purrai.management import Management
from purrai.models import ChatTurn, now
from test_chat import configured


async def check_status_consumers(env, client, chat, record_property, case, expected_error=""):
    settings, db, tg, service = env
    response = await client.get("/api/groups/-1001/chat")
    assert response.status_code == 200
    status = response.json()
    admin = BotAdmin(Management(db, tg, service, settings, chat=chat))
    await admin.chat_panel(1, -1001)
    panel = next(body["text"] for method, body in reversed(tg.calls) if method == "send_message")
    record_property("status_case", case)
    record_property("api_status", json.dumps(status, ensure_ascii=False))
    record_property("bot_panel", panel)
    assert status["error"] == (await chat.status(-1001))["error"] == expected_error
    if expected_error:
        assert f"最近问题：{expected_error}" in panel
    else:
        assert "最近问题：" not in panel
    return status


async def test_normal_silence_is_not_reported_as_chat_failure(env, client, record_property):
    chat, model = await configured(env)
    model.result = ChatReply(should_reply=False)
    await chat.observe(message(1, "柴郡喵，你怎么看"))
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "silent" and turn.error == "模型判断无需回复"
    assert not any(method == "send_message" for method, _ in chat.tg.calls)
    status = await check_status_consumers(env, client, chat, record_property, "silent")
    assert status["pending"] == 0


@pytest.mark.parametrize("failure_kind", ["model", "send"])
async def test_real_chat_failure_survives_a_newer_silent_turn(
    env, client, monkeypatch, record_property, failure_kind,
):
    chat, model = await configured(env)
    if failure_kind == "model":
        model.result = ChatModelError("离线模型调用失败")
    else:
        chat.tg.fail["send_message"] = 1
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        failed = await session.scalar(select(ChatTurn))
        assert failed.state == "failed" and failed.error
        failure = failed.error
    later = now() + 61
    monkeypatch.setattr("purrai.models.time.time", lambda: later)
    model.result = ChatReply(should_reply=False)
    await chat.observe(message(2, "柴郡喵，你觉得呢"))
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        latest = await session.scalar(select(ChatTurn).order_by(ChatTurn.id.desc()))
        assert latest.state == "silent" and latest.error == "模型判断无需回复"
    await check_status_consumers(env, client, chat, record_property, f"{failure_kind}_failure", failure)


@pytest.mark.parametrize("condition", ["cooldown", "quota"])
async def test_budget_waits_and_exhaustion_are_not_chat_failures(env, client, record_property, condition):
    chat, model = await configured(env)
    if condition == "cooldown":
        chat.settings.chat_user_interval = 5
    else:
        chat.settings.chat_daily_calls = 1
    await chat.budget.reserve(-1001, 42, "system", "context")
    await chat.observe(message(1, "柴郡喵，你怎么看"))
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == ("pending" if condition == "cooldown" else "skipped")
        assert turn.error
    assert not model.calls
    status = await check_status_consumers(env, client, chat, record_property, condition)
    assert status["pending"] == (1 if condition == "cooldown" else 0)
    assert status["budget"]["global_calls"] == 1
