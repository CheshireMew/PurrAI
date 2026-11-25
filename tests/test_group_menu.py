"""Menu settings through management, real feature routing and durable effects."""
import hashlib

import pytest
from aiogram.types import Message, ReplyKeyboardMarkup
from sqlalchemy import func, select

from conftest import drain, message
from purrai.group_menu import keyboard, matched_action
from purrai.models import Action, GroupMenu, PointsAccount, PointsEntry, Session
from purrai.runtime import Runtime
from purrai.telegram import telegram_data

PATH = "/api/groups/-1001/menu"


async def save(client, rows=None):
    doc = (await client.get(PATH)).json()
    config = {"rows": rows} if rows is not None else doc["config"]
    response = await client.put(PATH, json={"config": config, "version": doc["version"]},
        headers={"Idempotency-Key": f"menu-save-{doc['version']}"})
    assert response.status_code == 200, response.text
    return response.json()


async def operate(client, operation="publish", key=None):
    doc = (await client.get(PATH)).json()
    response = await client.post(PATH + "/operate", json={"operation": operation, "version": doc["version"]},
        headers={"Idempotency-Key": key or f"menu-{operation}-{doc['version']}"})
    assert response.status_code == 200, response.text
    return response.json()


def button(text, action, style="default"):
    return {"text": text, "action": action, "style": style}


def incoming(identifier, text):
    return Message.model_validate(message(identifier, text,
        **{"from": {"id": 42, "is_bot": False, "first_name": "群友"}}))


async def enable_points(client):
    path = "/api/groups/-1001/points/settings"
    doc = (await client.get(path)).json()
    doc["config"].update(enabled=True, chat_points=0)
    response = await client.put(path, json=doc, headers={"Idempotency-Key": "points-menu-enable"})
    assert response.status_code == 200, response.text


async def test_default_menu_publish_update_clear_and_republish(client, env):
    _, db, tg, service = env
    doc = (await client.get(PATH)).json()
    assert [item["action"] for item in doc["config"]["rows"][0]] == ["signin", "balance", "lottery"]
    assert doc["version"] == 0 and not doc["active"]
    # The shipped defaults are ordinary saved configuration, not an implicit live menu.
    async with db.sessions() as session:
        assert await matched_action(session, -1001, "签到") is None
    published = await operate(client)
    assert published["delivery"]["state"] == "pending" and not published["active"]
    await drain(service)
    doc = (await client.get(PATH)).json()
    assert doc["active"] and doc["delivery"]["state"] == "succeeded"
    sent = [args for method, args in tg.calls if method == "send_message"][-1]
    assert sent["reply_markup"]["is_persistent"]
    assert sent["reply_markup"]["keyboard"][0][0] == {"text": "签到", "style": "success"}
    # Exercise the actual installed SDK validation, including button style preservation.
    assert ReplyKeyboardMarkup.model_validate(sent["reply_markup"]).keyboard[0][0].style == "success"
    rows = [[button("活动入口", "lottery", "primary")], [button("我的余额", "balance"), button("每日报到", "signin", "success")]]
    await save(client, rows)
    assert (await client.get(PATH)).json()["needs_update"]
    await operate(client)
    await drain(service)
    async with db.sessions() as session:
        assert await matched_action(session, -1001, "每日报到") == "signin"
        assert await matched_action(session, -1001, "签到") is None
    await operate(client, "clear")
    await drain(service)
    doc = (await client.get(PATH)).json()
    assert not doc["active"] and doc["config"]["rows"] == rows
    assert [args for method, args in tg.calls if method == "send_message"][-1]["reply_markup"] == {"remove_keyboard": True}
    async with db.sessions() as session:
        assert await matched_action(session, -1001, "每日报到") is None
    await operate(client)
    await drain(service)
    assert (await client.get(PATH)).json()["active"]


async def test_custom_labels_use_existing_points_and_lottery_without_changing_message(client, env):
    _, db, tg, service = env
    await enable_points(client)
    await save(client, [[button("每日报到甲", "signin"), button("我的账户甲", "balance"), button("今日活动甲", "lottery")]])
    await operate(client)
    await drain(service)
    runtime = Runtime(db, tg, service)
    try:
        for identifier, label in [(300, "每日报到甲"), (300, "每日报到甲"), (301, "我的账户甲"), (302, "今日活动甲")]:
            await runtime.events.message(incoming(identifier, label))
        await drain(service)
        async with db.sessions() as session:
            assert await session.scalar(select(PointsAccount.balance)) == 10
            assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 1
            point = await session.scalar(select(Action).where(Action.dedupe_key == "points:-1001:300"))
            assert point.payload["message"]["text"] == "每日报到甲"
        replies = [args["text"] for method, args in tg.calls if method == "send_message"]
        assert sum("签到成功" in reply for reply in replies) == 1
        assert any("你当前有 10 积分" in reply for reply in replies)
        assert any("暂时没有进行中的抽奖" in reply for reply in replies)
    finally:
        await runtime.admin.ui.close()
        await runtime.chat.close()


async def test_clear_other_bot_keyboard_without_creating_configuration(client, env):
    _, db, tg, service = env
    await operate(client, "clear")
    await drain(service)
    assert [args for method, args in tg.calls if method == "send_message"][-1]["reply_markup"] == {"remove_keyboard": True}
    async with db.sessions() as session:
        row = await session.get(GroupMenu, -1001)
        assert row.active_config is None
        cleanup = await session.scalar(select(Action).where(Action.dedupe_key.startswith("cleanup:notice:")))
        assert cleanup and cleanup.available_at > cleanup.created_at


async def test_menu_operations_idempotency_conflicts_and_superseded_publish(client, env):
    _, db, tg, service = env
    first = await operate(client, key="publish-menu-once")
    repeat = await client.post(PATH + "/operate", json={"operation": "publish", "version": 0},
        headers={"Idempotency-Key": "publish-menu-once"})
    assert repeat.json() == first
    conflict = await client.post(PATH + "/operate", json={"operation": "clear", "version": first["version"]},
        headers={"Idempotency-Key": "publish-menu-once"})
    assert conflict.status_code == 409
    stale = await client.post(PATH + "/operate", json={"operation": "clear", "version": 0},
        headers={"Idempotency-Key": "stale-menu-request"})
    assert stale.status_code == 409
    cleared = await operate(client, "clear")
    await drain(service)
    async with db.sessions() as session:
        assert (await session.get(Action, first["delivery"]["id"])).state == "cancelled"
        assert (await session.get(Action, cleared["delivery"]["id"])).state == "succeeded"
    sends = [args for method, args in tg.calls if method == "send_message"]
    assert len(sends) == 1 and sends[0]["reply_markup"] == {"remove_keyboard": True}


async def test_saved_draft_cancels_pending_old_publish(client, env):
    _, db, tg, service = env
    first = await operate(client)
    await save(client, [[button("新的入口", "signin")]])
    await drain(service)
    async with db.sessions() as session:
        assert (await session.get(Action, first["delivery"]["id"])).state == "cancelled"
    assert not [args for method, args in tg.calls if method == "send_message"]


async def test_old_queued_button_actions_stop_after_menu_clear(client, env):
    _, db, tg, service = env
    await enable_points(client)
    await save(client, [[button("报到乙", "signin"), button("活动乙", "lottery")]])
    await operate(client)
    await drain(service)
    runtime = Runtime(db, tg, service)
    try:
        assert await runtime.events.points.observe(telegram_data(incoming(310, "报到乙")))
        assert await runtime.events.lottery.observe(telegram_data(incoming(311, "活动乙")))
        cleared = await operate(client, "clear")
        await service.actions.execute_claimed_action(cleared["delivery"]["id"])
        await drain(service)
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 0
            queued = (await session.scalars(select(Action).where(Action.dedupe_key.in_(
                ("points:-1001:310", "lottery-list:-1001:311"))))).all()
            assert len(queued) == 2 and all(row.state == "cancelled" for row in queued)
    finally:
        await runtime.admin.ui.close()
        await runtime.chat.close()


async def test_points_disabled_menu_has_explanation_and_no_reward(client, env):
    _, db, tg, service = env
    await operate(client)
    await drain(service)
    runtime = Runtime(db, tg, service)
    try:
        await runtime.events.message(incoming(320, "签到"))
        await drain(service)
        assert any("暂未开启积分" in args.get("text", "") for method, args in tg.calls if method == "send_message")
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 0
    finally:
        await runtime.admin.ui.close()
        await runtime.chat.close()


async def test_menu_delivery_failure_is_visible_and_not_silently_retried(client, env):
    _, db, tg, service = env
    tg.fail["send_message"] = 1
    first = await operate(client)
    await drain(service)
    doc = (await client.get(PATH)).json()
    assert not doc["active"] and doc["delivery"]["state"] == "failed"
    assert "未确认" in doc["delivery"]["error"]
    # A fresh explicit update is safe after the administrator checks Telegram.
    second = await operate(client)
    await drain(service)
    assert second["delivery"]["id"] != first["delivery"]["id"]
    assert (await client.get(PATH)).json()["active"]


async def test_menu_permission_is_group_scoped_and_checked_at_execution(client, env):
    _, db, tg, service = env
    async with db.transaction() as session:
        login = await session.get(Session, hashlib.sha256(b"test-session").hexdigest())
        login.user_id = 2
    assert (await client.get(PATH)).status_code == 403
    tg.members[(-1001, 2)] = {"status": "administrator"}
    doc = await operate(client)
    tg.members[(-1001, 2)] = {"status": "member"}
    await drain(service)
    async with db.sessions() as session:
        assert (await session.get(Action, doc["delivery"]["id"])).state == "cancelled"
    assert not [args for method, args in tg.calls if method == "send_message"]


@pytest.mark.parametrize("rows", [[], [[]], [[button("x", "unknown")]],
    [[button("x", "signin", "purple")]], [[button("ＡＢ", "signin"), button("ab", "balance")]],
    [[button(" /help", "signin")]], [[button("two\nlines", "signin")]]])
async def test_invalid_menu_configuration_is_rejected(client, rows):
    response = await client.put(PATH, json={"config": {"rows": rows}, "version": 0},
        headers={"Idempotency-Key": "invalid-menu-config"})
    assert response.status_code == 422


def test_default_style_uses_client_theme():
    assert keyboard({"rows": [[button("任意标签", "balance")]]})["keyboard"] == [[{"text": "任意标签"}]]
