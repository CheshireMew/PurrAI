"""Lottery configuration, money movements, draws, reuse and recovery."""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from conftest import drain, message
from purrai import lottery
from purrai.db import Database
from purrai.lottery import BEIJING, cycle, participate
from purrai.lottery_bot import LotteryBot
from purrai.models import Lottery, LotteryEntry, PointsAccount, PointsDaily, PointsEntry, now
from purrai.points_rewards import user_account
from purrai.points_schemas import MAX_POINTS

BASE = "/api/groups/-1001/lotteries"
USER = {"id": 42, "first_name": "群友甲", "is_bot": False}


async def enable_points(client):
    path = "/api/groups/-1001/points/settings"
    doc = (await client.get(path)).json()
    doc["config"]["enabled"] = True
    result = await client.put(path, json=doc, headers={"Idempotency-Key": "lottery-enable-points"})
    assert result.status_code == 200, result.text


async def seed(db, user=42, balance=1000):
    async with db.transaction() as session:
        account = await user_account(session, -1001, {"id": user, "first_name": f"群友{user}"})
        account.balance = balance


async def balance(db, user=42):
    async with db.sessions() as session:
        return await session.scalar(select(PointsAccount.balance).where(
            PointsAccount.group_id == -1001, PointsAccount.user_id == user))


async def create(client, key="create-lottery", **changes):
    config = {"title": "群庆抽奖", "prizes": [{"name": "会员月卡", "kind": "manual", "quantity": 1}], **changes}
    response = await client.post(BASE, json={"config": config}, headers={"Idempotency-Key": key})
    assert response.status_code == 200, response.text
    return response.json()


async def operate(client, row, operation, key=None, status=200, **changes):
    response = await client.post(f"{BASE}/{row['id']}/operate",
        json={"operation": operation, "version": row["version"], **changes},
        headers={"Idempotency-Key": key or f"{operation}:{row['id']}:{row['version']}"})
    assert response.status_code == status, response.text
    return response.json()


async def join(db, row, user=42, key=None):
    async with db.transaction() as session:
        entry = await participate(session, await session.get(Lottery, row["id"]),
            {"id": user, "first_name": f"群友{user}"}, key or f"join:{user}")
        await session.flush()
        return {"id": entry.id, "state": entry.state, "prize_index": entry.prize_index, "fulfilled": entry.fulfilled}


async def test_free_raffle_unique_members_and_manual_prize_delivery(client, env):
    _, db, tg, service = env
    row = await operate(client, await create(client), "publish")
    assert row["state"] == "open"
    a, repeated = await join(db, row), await join(db, row, key="another-click")
    assert a["id"] == repeated["id"]
    await join(db, row, user=43)
    row = await operate(client, row, "draw")
    assert row["state"] == "drawn" and row["participant_count"] == 2
    detail = (await client.get(f"{BASE}/{row['id']}")).json()
    winners = [entry for entry in detail["entries"] if entry["state"] == "won"]
    assert len(winners) == 1 and not winners[0]["fulfilled"]
    await operate(client, row, "fulfill", entry_id=winners[0]["id"])
    await drain(service)
    assert any("已开奖" in data.get("text", "") for method, data in tg.calls if method == "send_message")
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 0


async def test_paid_join_gate_does_not_charge_gate_and_cancel_refunds_once(client, env):
    _, db, _, _ = env
    await enable_points(client)
    await seed(db, balance=500)
    row = await operate(client, await create(client, entry_cost=100, min_balance=500), "publish")
    await asyncio.gather(join(db, row, key="click-one"), join(db, row, key="click-two"))
    assert await balance(db) == 400
    cancelled = await operate(client, row, "cancel", key="cancel-one")
    assert await balance(db) == 500
    assert await operate(client, row, "cancel", key="cancel-one") == cancelled
    async with db.transaction() as session:
        await cycle(session)
        assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 2
    assert await balance(db) == 500


async def test_insufficient_balance_or_gate_never_charges(client, env):
    _, db, _, _ = env
    await enable_points(client)
    await seed(db, balance=90)
    row = await operate(client, await create(client, entry_cost=100, min_balance=500), "publish")
    from purrai.errors import ManagementError
    with pytest.raises(ManagementError, match="至少需要 500"):
        await join(db, row)
    assert await balance(db) == 90
    assert not (await client.get(f"{BASE}/{row['id']}")).json()["entries"]


async def test_recent_active_days_can_be_combined_with_free_and_points_gate(client, env):
    _, db, _, _ = env
    await enable_points(client)
    await seed(db, balance=500)
    today = datetime.fromtimestamp(now(), BEIJING).date()
    async with db.transaction() as session:
        for offset, points in [(0, 1), (1, 2), (2, 0), (9, 50)]:
            session.add(PointsDaily(group_id=-1001, user_id=42, day=(today - timedelta(days=offset)).isoformat(),
                                    chat_points=points))
    row = await operate(client, await create(client, min_balance=500, min_active_days=3, active_window_days=7), "publish")
    from purrai.errors import ManagementError
    with pytest.raises(ManagementError, match="目前 2 天"):
        await join(db, row)
    async with db.transaction() as session:
        (await session.get(PointsDaily, (-1001, 42, (today - timedelta(days=2)).isoformat()))).chat_points = 1
    await join(db, row)
    assert await balance(db) == 500


async def test_target_count_draws_once_and_awards_points_atomically(client, env):
    _, db, _, _ = env
    await enable_points(client)
    for user in (42, 43, 44):
        await seed(db, user=user, balance=1000)
    row = await operate(client, await create(client, entry_cost=100, draw_mode="participants", participant_target=3,
        prizes=[{"name": "贡献奖励", "kind": "points", "points": 200, "quantity": 2}]), "publish")
    await asyncio.gather(*(join(db, row, user=user) for user in (42, 43, 44)))
    detail = (await client.get(f"{BASE}/{row['id']}")).json()
    assert detail["state"] == "drawn" and detail["draw_count"] == 3
    assert len([entry for entry in detail["entries"] if entry["state"] == "won"]) == 2
    assert sum([await balance(db, user) for user in (42, 43, 44)]) == 3100
    await operate(client, detail, "draw", status=409)


@pytest.mark.parametrize("participants,enough", [(0, False), (1, False), (2, True)])
async def test_timed_raffle_recovers_after_restart_and_refunds_underfilled(client, env, monkeypatch, participants, enough):
    _, db, _, _ = env
    await enable_points(client)
    deadline = now() + 60
    row = await operate(client, await create(client, entry_cost=100, draw_mode="time", ends_at=deadline, min_participants=2), "publish")
    for user in range(42, 42 + participants):
        await seed(db, user=user)
        await join(db, row, user=user)
    monkeypatch.setattr(lottery, "now", lambda: deadline + 1)
    reopened = Database(db.url)
    try:
        async with reopened.transaction() as session:
            await cycle(session)
        async with reopened.transaction() as session:
            await cycle(session)
    finally:
        await reopened.close()
    detail = (await client.get(f"{BASE}/{row['id']}")).json()
    assert detail["state"] == ("drawn" if enough else "cancelled")
    for user in range(42, 42 + participants):
        assert await balance(db, user) == (900 if enough else 1000)


async def test_instant_stock_daily_limit_idempotency_and_end(client, env, monkeypatch):
    _, db, _, _ = env
    await enable_points(client)
    await seed(db)
    row = await operate(client, await create(client, mode="instant", entry_cost=50, daily_limit=2,
        prizes=[{"name": "积分奖", "kind": "points", "points": 100, "quantity": 1, "weight": 1},
                {"name": "礼品", "kind": "manual", "quantity": 1, "weight": 1},
                {"name": "未中奖", "kind": "none", "quantity": 0, "weight": 8}]), "publish")
    monkeypatch.setattr(lottery.secrets, "randbelow", lambda _: 0)
    first = await join(db, row, key="instant-first")
    assert first["state"] == "won" and first["fulfilled"]
    assert await join(db, row, key="instant-first") == first
    assert await balance(db) == 1050
    assert (await join(db, row, key="instant-second"))["state"] == "lost"  # depleted slot retains its probability
    assert await balance(db) == 1000
    from purrai.errors import ManagementError
    with pytest.raises(ManagementError, match="达到每日"):
        await join(db, row, key="instant-third")
    current = now()
    monkeypatch.setattr(lottery, "now", lambda: current + 86400)
    monkeypatch.setattr(lottery.secrets, "randbelow", lambda _: 1)
    assert (await join(db, row, key="instant-next-day"))["state"] == "won"
    with pytest.raises(ManagementError, match="奖品已全部抽完"):
        await join(db, row, key="instant-out-of-stock")
    assert await balance(db) == 950
    closed = await operate(client, row, "close")
    assert closed["draw_count"] == 3 and closed["participant_count"] == 1
    assert await balance(db) == 950


async def test_instant_concurrent_last_prize_not_overdrawn(client, env, monkeypatch):
    _, db, _, _ = env
    await enable_points(client)
    for user in (42, 43):
        await seed(db, user=user)
    row = await operate(client, await create(client, mode="instant", entry_cost=50,
        prizes=[{"name": "最后一份", "kind": "points", "points": 100, "quantity": 1}]), "publish")
    monkeypatch.setattr(lottery.secrets, "randbelow", lambda _: 0)
    results = await asyncio.gather(join(db, row, user=42), join(db, row, user=43), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum([await balance(db, user) for user in (42, 43)]) == 2050


async def test_timed_instant_ends_without_refunding_completed_draws(client, env, monkeypatch):
    _, db, _, _ = env
    await enable_points(client)
    await seed(db)
    deadline = now() + 60
    row = await operate(client, await create(client, mode="instant", entry_cost=50, draw_mode="time", ends_at=deadline), "publish")
    await join(db, row)
    monkeypatch.setattr(lottery, "now", lambda: deadline + 1)
    async with db.transaction() as session:
        await cycle(session)
    assert (await client.get(f"{BASE}/{row['id']}")).json()["state"] == "closed"
    assert await balance(db) == 950


async def test_copy_history_preserves_config_but_resets_people_results_stock_and_time(client, env):
    _, db, _, _ = env
    row = await operate(client, await create(client, draw_mode="time", ends_at=now() + 600), "publish")
    await join(db, row)
    row = await operate(client, row, "draw")
    copied = await operate(client, row, "copy", key="copy-past-draw")
    assert copied == await operate(client, row, "copy", key="copy-past-draw")
    assert copied["id"] != row["id"] and copied["state"] == "draft"
    assert copied["config"] == {**row["config"], "ends_at": 0}
    assert copied["participant_count"] == copied["draw_count"] == 0 and copied["message_id"] is None
    detail = (await client.get(f"{BASE}/{copied['id']}")).json()
    assert detail["entries"] == [] and detail["remaining"] == [1]
    await operate(client, copied, "publish", status=422)
    copied["config"]["ends_at"] = now() + 1200
    response = await client.put(f"{BASE}/{copied['id']}", json={"config": copied["config"], "version": copied["version"]},
                                headers={"Idempotency-Key": "schedule-copied-draw"})
    assert response.status_code == 200, response.text
    published = await operate(client, response.json(), "publish")
    await join(db, published)
    assert (await client.get(f"{BASE}/{row['id']}")).json()["state"] == "drawn"


async def test_published_config_immutable_and_stale_or_conflicting_requests_rejected(client, env):
    draft = await create(client)
    assert await create(client) == draft
    row = await operate(client, draft, "publish")
    response = await client.put(f"{BASE}/{row['id']}", json={"config": {**row["config"], "entry_cost": 500}, "version": row["version"]},
                                headers={"Idempotency-Key": "change-published"})
    assert response.status_code == 409
    await operate(client, draft, "cancel", status=409)
    await operate(client, draft, "cancel", key=f"publish:{draft['id']}:{draft['version']}", status=409)
    assert (await client.get(f"{BASE}/{row['id']}")).json()["config"]["entry_cost"] == 0


async def test_refund_and_points_reward_pending_at_balance_cap_recover_once(client, env):
    _, db, _, _ = env
    await enable_points(client)
    await seed(db, balance=MAX_POINTS)
    row = await operate(client, await create(client, entry_cost=100), "publish")
    await join(db, row)
    await seed(db, balance=MAX_POINTS)
    await operate(client, row, "cancel")
    detail = (await client.get(f"{BASE}/{row['id']}")).json()
    assert detail["entries"][0]["state"] == "refund_pending"
    await seed(db, balance=100)
    async with db.transaction() as session:
        await cycle(session)
        await cycle(session)
    assert await balance(db) == 200
    reward = await operate(client, await create(client, key="reward-cap", prizes=[
        {"name": "积分", "kind": "points", "points": 100, "quantity": 1}]), "publish")
    await join(db, reward)
    await seed(db, balance=MAX_POINTS)
    await operate(client, reward, "draw")
    assert not (await client.get(f"{BASE}/{reward['id']}")).json()["entries"][0]["fulfilled"]
    await seed(db, balance=100)
    async with db.transaction() as session:
        await cycle(session)
        await cycle(session)
    assert await balance(db) == 200


async def test_buttons_check_group_membership_and_retry_after_lost_ack(client, env):
    _, db, tg, service = env
    await enable_points(client)
    await seed(db)
    row = await operate(client, await create(client, entry_cost=100), "publish")
    bot = LotteryBot(db, tg)
    query = {"id": "callback-query-first", "from": USER, "message": {"chat": {"id": -1001}}, "data": f"lot:join:{row['id']}"}
    tg.members[(-1001, 42)] = {"status": "left"}
    assert await bot.callback(query)
    assert await balance(db) == 1000
    tg.members[(-1001, 42)] = {"status": "member"}
    tg.fail["answer_callback_query"] = 1
    assert await bot.callback(query)
    assert await balance(db) == 900
    assert await bot.callback(query)
    assert await balance(db) == 900
    assert await bot.callback({**query, "id": "query-other-group", "message": {"chat": {"id": -1002}}})
    assert await balance(db) == 900
    await drain(service)
    assert any("报名消耗 100" in data.get("text", "") for method, data in tg.calls)
    await bot.close()


async def test_notice_uncertain_delivery_not_repeated_and_result_survives(client, env):
    _, db, tg, service = env
    row = await operate(client, await create(client), "publish")
    tg.fail["send_message"] = 1
    await drain(service)
    detail = (await client.get(f"{BASE}/{row['id']}")).json()
    assert detail["deliveries"][0]["state"] == "failed" and "未确认" in detail["deliveries"][0]["error"]
    await drain(service)
    assert len([call for call in tg.calls if call[0] == "send_message"]) == 1
    await join(db, row)
    await operate(client, row, "draw")
    await drain(service)
    assert len([call for call in tg.calls if call[0] == "send_message"]) == 2


async def test_lottery_list_command_and_rules_button_use_existing_event_routes(client, env):
    from aiogram.types import Message
    from purrai.runtime import Runtime
    from test_bot_admin import deliver
    _, db, tg, service = env
    row = await operate(client, await create(client), "publish")
    runtime = Runtime(db, tg, service)
    try:
        tg.username = "PurrAITestBot"
        assert not await runtime.events.lottery.observe(message(980, "/lottery@OtherBot", **{"from": USER}))
        await runtime.events.message(Message.model_validate(message(981, "/lottery", **{"from": USER})))
        await drain(service)
        assert any(f"lot:rules:{row['id']}" in str(data.get("reply_markup")) for method, data in tg.calls)
        query = {"id": "rules-query", "chat_instance": "lottery-test", "from": dict(USER),
            "message": message(982, "群庆抽奖"), "data": f"lot:rules:{row['id']}"}
        await deliver(runtime, {"callback_query": query})
        await drain(service)
        assert any("免费报名，每人一次机会" in data.get("text", "") for method, data in tg.calls)
        query.update(id="join-query", data=f"lot:join:{row['id']}")
        await deliver(runtime, {"callback_query": query})
        await deliver(runtime, {"callback_query": query})
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(LotteryEntry)) == 1
    finally:
        await runtime.stop()


async def test_unauthorized_admin_cannot_write(client, env):
    from purrai.models import Session
    _, db, _, _ = env
    async with db.transaction() as session:
        user_session = await session.scalar(select(Session))
        user_session.user_id = 77
    response = await client.post(BASE, json={"config": {"title": "越权", "prizes": [{"name": "奖品"}]}},
                                 headers={"Idempotency-Key": "unauthorized-write"})
    assert response.status_code == 403


async def test_private_bot_create_edit_publish_draw_fulfill_and_reuse(env):
    from purrai.runtime import Runtime
    from test_bot_admin import click, deliver, markup, open_group, private
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    try:
        await open_group(runtime, tg)
        await click(runtime, tg, "抽奖管理")
        await click(runtime, tg, "新建免费报名抽奖")
        await deliver(runtime, {"message": private("周末福利")})
        await deliver(runtime, {"message": private("奖品 月卡 1")})
        assert "周末福利" in markup(tg)["text"]
        await click(runtime, tg, "修改标题")
        await deliver(runtime, {"message": private("周末福利第二版")})
        await click(runtime, tg, "发布到本群")
        await click(runtime, tg, "确认")
        async with db.sessions() as session:
            stored = await session.scalar(select(Lottery))
            row = {"id": stored.id}
            assert stored.state == "open" and stored.config["title"] == "周末福利第二版"
        await join(db, row)
        await click(runtime, tg, "立即开奖")
        await click(runtime, tg, "确认")
        await click(runtime, tg, "登记已发")
        await click(runtime, tg, "确认")
        await click(runtime, tg, "复用配置")
        async with db.sessions() as session:
            rows = (await session.scalars(select(Lottery))).all()
            assert len(rows) == 2 and {row.state for row in rows} == {"drawn", "draft"}
            assert (await session.scalar(select(LotteryEntry))).fulfilled
    finally:
        await runtime.stop()
