import json

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.models import Action, Inbox, Meta, now
from purrai.runtime_alerts import RuntimeAlerts, update_episode
from purrai.runtime_notice_delivery import requeue_uncertain_notices


async def make_notice(db):
    async with db.transaction() as session:
        await update_episode(session, "连接检查", "查询成员连续超时", [1])
        return await session.scalar(select(Action.id).where(Action.kind == "owner_alert"))


async def test_uncertain_owner_notice_retries_with_duplicate_label_and_keeps_receipts(env):
    _, db, tg, service = env
    identifier = await make_notice(db)
    tg.fail["send_message"] = 1
    assert await service.run_one_action()
    async with db.transaction() as session:
        action = await session.get(Action, identifier)
        assert action.state == "pending" and action.payload["delivery"]["state"] == "uncertain"
        assert action.available_at > now() + 20
        action.available_at = now() - 1
    assert await service.run_one_action()
    async with db.sessions() as session:
        action = await session.get(Action, identifier)
        assert action.state == "succeeded" and action.payload["delivery"]["state"] == "confirmed"
        assert len(action.payload["notice_delivery_history"]) == 1
    sent = [args for method, args in tg.calls if method == "send_message"]
    assert len(sent) == 2 and sent[-1]["text"].startswith("补发运行提醒")
    assert not await service.run_one_action()


async def test_owner_notice_stops_after_eight_attempts_and_remains_visible_without_recursion(env):
    settings, db, tg, service = env
    identifier = await make_notice(db)
    tg.fail["send_message"] = 8
    for _ in range(8):
        async with db.transaction() as session:
            (await session.get(Action, identifier)).available_at = now() - 1
        assert await service.run_one_action()
    await RuntimeAlerts(db, settings).cycle()
    async with db.sessions() as session:
        action = await session.get(Action, identifier)
        assert action.state == "failed" and action.attempts == 8
        assert "停止自动补发" in action.error
        assert json.loads((await session.get(Meta, "runtime-alert:提醒发送")).value)["active"]
        assert len((await session.scalars(select(Action))).all()) == 1


async def test_obsolete_notice_cancelled_and_only_latest_state_is_sent(env):
    _, db, tg, service = env
    identifier = await make_notice(db)
    tg.fail["send_message"] = 1
    assert await service.run_one_action()
    async with db.transaction() as session:
        await update_episode(session, "连接检查", "", [1], recovery="已正常收到查询响应")
        (await session.get(Action, identifier)).available_at = now() - 1
    assert await service.run_one_action()
    assert await service.run_one_action()
    async with db.sessions() as session:
        assert (await session.get(Action, identifier)).state == "cancelled"
    sent = [args["text"] for method, args in tg.calls if method == "send_message"]
    assert len(sent) == 2 and "已正常收到查询响应" in sent[-1]


async def test_legacy_requeue_is_only_for_operational_notices(env):
    _, db, _, _ = env
    async with db.transaction() as session:
        session.add(Meta(key="runtime-alert:连接检查", value=json.dumps({"episode": 1, "active": True})))
        for kind in ("owner_alert", "challenge", "admin_notify", "join_link"):
            await enqueue(session, kind, "owner-alert:连接检查:1:True:1" if kind == "owner_alert" else kind,
                payload={"text": "运行状态", "chat_id": 1, "delivery": {"state": "uncertain"}})
        for action in await session.scalars(select(Action)):
            action.state, action.attempts, action.updated_at = "failed", 1, now() - 60
        await session.flush()
        await requeue_uncertain_notices(session)
    async with db.sessions() as session:
        states = {action.kind: action.state for action in await session.scalars(select(Action))}
        assert states == {"owner_alert": "pending", "challenge": "failed", "admin_notify": "failed", "join_link": "failed"}


async def test_confirmed_receipt_after_restart_does_not_resend(env):
    _, db, tg, service = env
    identifier = await make_notice(db)
    async with db.transaction() as session:
        action = await session.get(Action, identifier)
        action.payload = {**action.payload, "delivery": {"state": "confirmed", "result": {"message_id": 12}}}
    assert await service.run_one_action()
    assert not any(method == "send_message" for method, _ in tg.calls)


async def test_crash_after_final_send_cannot_bypass_notice_attempt_limit(env):
    _, db, tg, service = env
    identifier = await make_notice(db)
    async with db.transaction() as session:
        action = await session.get(Action, identifier)
        action.attempts = 8
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
