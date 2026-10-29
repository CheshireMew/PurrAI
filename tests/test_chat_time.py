"""Each actual model request gets current Beijing time without breaking delivery reuse."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from purrai.chat_generation import ChatGeneration
from purrai.chat_model import ChatModel, ChatReply
from purrai.config import Settings
from purrai.schemas import GroupSettings
from test_chat import FakeModel


def timestamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def turn(identifier=1):
    return SimpleNamespace(id=identifier, group_id=-1001, topic_id=0, message_id=identifier, response="")


def budget():
    return SimpleNamespace(reserve=AsyncMock(return_value="reservation"), settle=AsyncMock())


async def generate(generation, record):
    return await generation.generate(record, GroupSettings(),
                                     [{"id": 1, "text": "现在北京时间多少？", "time": "01-01 01:00:00"}],
                                     42, {})


@pytest.mark.parametrize("utc,expected", [
    ("2026-10-28T07:54:00", "2026-10-28 15:54:00 星期三"),
    ("2026-10-28T16:00:01", "2026-10-29 00:00:01 星期四"),
    ("2026-12-31T16:00:00", "2027-01-01 00:00:00 星期五"),
])
async def test_request_uses_current_beijing_date_and_time_instead_of_message_time(monkeypatch, utc, expected):
    monkeypatch.setattr("purrai.chat_generation.now", lambda: timestamp(utc))
    model, accounting = FakeModel(), budget()
    await generate(ChatGeneration(model, accounting), turn())
    system, payload = model.calls[0]
    assert payload["current_time"] == expected + "（北京时间，Asia/Shanghai，UTC+08:00）"
    assert payload["messages"][0]["time"] == "01-01 01:00:00"
    assert "current_time 是本次请求时的北京时间" in system
    assert json.loads(accounting.reserve.await_args.args[3]) == payload


async def test_new_requests_refresh_time_across_midnight(monkeypatch):
    clock = [timestamp("2026-10-28T15:59:59")]
    monkeypatch.setattr("purrai.chat_generation.now", lambda: clock[0])
    model = FakeModel()
    generation = ChatGeneration(model, budget())
    await generate(generation, turn(1))
    clock[0] += 2
    await generate(generation, turn(2))
    assert model.calls[0][1]["current_time"].startswith("2026-10-28 23:59:59 星期三")
    assert model.calls[1][1]["current_time"].startswith("2026-10-29 00:00:01 星期四")
    assert model.calls[0][0] == model.calls[1][0]


async def test_delivery_retry_reuses_reply_even_when_clock_changes(monkeypatch):
    clock = [timestamp("2026-10-28T15:59:59")]
    monkeypatch.setattr("purrai.chat_generation.now", lambda: clock[0])
    model, accounting, record = FakeModel(ChatReply("已生成的回复")), budget(), turn()
    generation = ChatGeneration(model, accounting)
    first = await generate(generation, record)
    record.response = json.dumps({"context": first.fingerprint, "text": first.result.text, "is_advertisement": False})
    clock[0] += 2
    second = await generate(generation, record)
    assert second.result.text == first.result.text and second.fingerprint == first.fingerprint
    assert len(model.calls) == accounting.reserve.await_count == accounting.settle.await_count == 1


async def test_current_time_reaches_the_actual_outgoing_model_http_body(monkeypatch):
    monkeypatch.setattr("purrai.chat_generation.now", lambda: timestamp("2026-10-28T07:54:00"))
    sent = []

    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "is_advertisement": False, "should_reply": True, "reply": "北京时间", "sticker": None,
        })}}], "usage": {"total_tokens": 5}})

    model = ChatModel(Settings(_env_file=None, chat_api_base="https://model.test/v1", chat_model="test"),
                      transport=httpx.MockTransport(handler))
    try:
        await generate(ChatGeneration(model, budget()), turn())
    finally:
        await model.close()
    payload = json.loads(sent[0]["messages"][1]["content"])
    assert payload["current_time"] == "2026-10-28 15:54:00 星期三（北京时间，Asia/Shanghai，UTC+08:00）"
