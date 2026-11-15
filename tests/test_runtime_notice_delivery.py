import json

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import Action, Inbox, Meta, now
from purrai.runtime_alerts import RuntimeAlerts, update_episode
from purrai.runtime_notice_delivery import reconcile_notices


async def make_notice(db):
    async with db.transaction() as session:
        await update_episode(session, "连接检查", "查询成员连续超时", [1])
        return await session.scalar(select(Action.id).where(Action.kind == "owner_alert"))


async def test_uncertain_owner_notice_is_not_sent_again(env):
    settings, db, tg, service = env
    identifier = await make_notice(db)
    tg.fail["send_message"] = 1
    assert await service.run_one_action()
    async with db.transaction() as session:
        action = await session.get(Action, identifier)
        assert action.state == "failed" and action.payload["delivery"]["state"] == "uncertain"
        action.available_at = now() - 1
    await RuntimeAlerts(db, settings).cycle()
    assert not await service.run_one_action()
    async with db.sessions() as session:
        action = await session.get(Action, identifier)
        assert action.state == "failed" and action.attempts == 1
    sent = [args for method, args in tg.calls if method == "send_message"]
    assert len(sent) == 1
    assert not await service.run_one_action()


async def test_failed_notice_stays_visible_without_repeated_or_recursive_messages(env):
    settings, db, tg, service = env
    identifier = await make_notice(db)
    tg.fail["send_message"] = 1
    assert await service.run_one_action()
    for _ in range(3):
        await RuntimeAlerts(db, settings).cycle()
        assert not await service.run_one_action()
    async with db.sessions() as session:
        action = await session.get(Action, identifier)
        assert action.state == "failed" and action.attempts == 1
        assert "不再补发" in action.error
        assert json.loads((await session.get(Meta, "runtime-alert:提醒发送")).value)["active"]
        assert len((await session.scalars(select(Action))).all()) == 1


async def test_recovery_cancels_old_notice_without_sending_any_new_message(env):
    settings, db, tg, service = env
    identifier = await make_notice(db)
    tg.fail["send_message"] = 1
    assert await service.run_one_action()
    async with db.transaction() as session:
        await update_episode(session, "连接检查", "", [1], recovery="已正常收到查询响应")
        (await session.get(Action, identifier)).available_at = now() - 1
    await RuntimeAlerts(db, settings).cycle()
    assert not await service.run_one_action()
    async with db.sessions() as session:
        assert (await session.get(Action, identifier)).state == "cancelled"
    sent = [args["text"] for method, args in tg.calls if method == "send_message"]
    assert len(sent) == 1 and "已正常收到查询响应" not in sent[-1]


async def test_maintenance_does_not_requeue_uncertain_notices_or_other_effects(env):
    _, db, _, _ = env
    async with db.transaction() as session:
        session.add(Meta(key="runtime-alert:连接检查", value=json.dumps({"episode": 1, "active": True})))
        for kind in ("owner_alert", "challenge", "admin_notify", "join_link"):
            await enqueue(session, kind, "owner-alert:连接检查:1:True:1" if kind == "owner_alert" else kind,
                payload={"text": "运行状态", "chat_id": 1, "delivery": {"state": "uncertain"}})
        for action in await session.scalars(select(Action)):
            action.state, action.attempts, action.updated_at = "failed", 1, now() - 60
        await session.flush()
        await reconcile_notices(session)
    async with db.sessions() as session:
        states = {action.kind: action.state for action in await session.scalars(select(Action))}
        assert states == {"owner_alert": "failed", "challenge": "failed", "admin_notify": "failed", "join_link": "failed"}


async def test_old_poll_recovery_is_cancelled_instead_of_resent(env):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _, db, tg, service = env
    occurred_at = datetime(2026, 11, 15, 8, 27, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    original = "PurrAI · 已恢复\nTelegram 收件"
    async with db.transaction() as session:
        session.add(Meta(key="runtime-alert:Telegram 收件", value=json.dumps({"episode": 1, "active": False})))
        action = await enqueue(session, "owner_alert", "owner-alert:Telegram 收件:1:False:1",
            payload={"chat_id": 1, "text": original, "episode": 1, "delivery": {"state": "uncertain"}})
        action.state, action.attempts = "failed", 1
        action.created_at, action.updated_at = occurred_at, now() - 60
        identifier = action.id
        await session.flush()
        await reconcile_notices(session)
    assert not await service.run_one_action()
    sent = [kw["text"] for method, kw in tg.calls if method == "send_message"]
    assert not sent
    async with db.sessions() as session:
        action = await session.get(Action, identifier)
        assert action.payload["text"] == original and action.state == "cancelled"


async def test_confirmed_receipt_after_restart_does_not_resend(env):
    _, db, tg, service = env
    identifier = await make_notice(db)
    async with db.transaction() as session:
        action = await session.get(Action, identifier)
        action.payload = {**action.payload, "delivery": {"state": "confirmed", "result": {"message_id": 12}}}
    assert await service.run_one_action()
    assert not any(method == "send_message" for method, _ in tg.calls)


async def test_crash_during_notice_send_does_not_repeat_it_after_restart(env):
    _, db, tg, service = env
    identifier = await make_notice(db)
    async with db.transaction() as session:
        action = await session.get(Action, identifier)
        action.attempts = 1
        action.payload = {**action.payload, "delivery": {"state": "sending"}}
    assert await service.run_one_action()
    assert not any(method == "send_message" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.get(Action, identifier)).state == "failed"


async def test_permanently_rejected_notice_is_visible_without_automatic_retry(env):
    settings, db, _, _ = env
    identifier = await make_notice(db)
    async with db.transaction() as session:
        action = await session.get(Action, identifier)
        action.state = "failed"
        action.payload = {**action.payload, "delivery": {"state": "rejected"}}
        action.error = "TelegramForbiddenError"
    await RuntimeAlerts(db, settings).cycle()
    async with db.sessions() as session:
        assert (await session.get(Action, identifier)).state == "failed"
        assert json.loads((await session.get(Meta, "runtime-alert:提醒发送")).value)["active"]
        assert len((await session.scalars(select(Action))).all()) == 1


async def test_failed_inbox_is_alerted_and_records_the_specific_event(env):
    settings, db, _, _ = env
    async with db.transaction() as session:
        session.add(Inbox(id=321, payload={"update_id": 321}, state="failed", attempts=8))
    alerts = RuntimeAlerts(db, settings)
    await alerts.cycle()
    async with db.transaction() as session:
        marker = json.loads((await session.get(Meta, "runtime-alert:消息处理失败")).value)
        assert marker["active"] and marker["details"]["event_ids"] == [321]
        assert "达到自动重试上限" in marker["message"]
        (await session.get(Inbox, 321)).state = "done"
    await alerts.cycle()
    async with db.sessions() as session:
        assert not json.loads((await session.get(Meta, "runtime-alert:消息处理失败")).value)["active"]
