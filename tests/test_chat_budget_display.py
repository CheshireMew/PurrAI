import json

from purrai.chat_budget import ChatBudget, KEY
from purrai.models import Group, Meta
from purrai.runtime import Runtime
from test_bot_admin import markup


async def test_panel_labels_group_totals_and_refresh_time_without_consuming_budget(env, monkeypatch):
    settings, db, tg, service = env
    settings.chat_group_daily_calls = 1000
    settings.chat_group_daily_tokens = 2000000
    data = {"day": ChatBudget.day(), "global": {"calls": 60, "tokens": 200000},
            "groups": {"-1001": {"calls": 53, "tokens": 138471}}, "users": {"1": {"calls": 1, "tokens": 17}}}
    async with db.transaction() as session:
        session.add(Meta(key=KEY, value=json.dumps(data)))
    runtime = Runtime(db, tg, service)
    monkeypatch.setattr("purrai.bot_configuration_panels.now", lambda: 1792494056)
    await runtime.admin.chat_panel(1, -1001)
    text = markup(tg)["text"]
    assert "统计刷新：2026-10-20 19:00:56（北京时间；点击刷新才更新）" in text
    assert "本群今日累计请求：已用 53 次 / 上限 1000 次" in text
    assert "本群今日 Token 额度占用：138,471 / 2,000,000" in text
    assert "全群所有成员和话题" in text and "请求预留" in text and "结算后可能回落" in text
    monkeypatch.setattr("purrai.bot_configuration_panels.now", lambda: 1792494066)
    await runtime.admin.chat_panel(1, -1001, message_id=123)
    assert "2026-10-20 19:01:06" in markup(tg)["text"]
    async with db.sessions() as session:
        assert json.loads((await session.get(Meta, KEY)).value) == data


async def test_inflight_token_reservation_falls_on_settlement_but_request_count_stays(env):
    settings, db, _, _ = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_enabled": True}
    budget = ChatBudget(db, settings)
    reservation = await budget.reserve(-1001, 41, "system", "历史上下文" * 1000)
    inflight = await budget.status(-1001)
    assert inflight["group_calls"] == 1
    assert inflight["group_tokens"] == reservation.tokens > 5990
    await budget.settle(reservation, 5990)
    settled = await budget.status(-1001)
    assert settled["group_calls"] == inflight["group_calls"]
    assert settled["group_tokens"] == 5990 < inflight["group_tokens"]
