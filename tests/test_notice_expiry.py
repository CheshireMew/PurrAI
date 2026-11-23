import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, Group, now
from purrai.notice_cleanup import NoticeCleanup
from purrai.service import Service


@pytest.mark.parametrize("command,submission", [("/unmute", False), ("/unban", False), ("/unmute", True)])
async def test_group_command_and_all_enabled_receipts_use_durable_cleanup(env, monkeypatch, command, submission):
    from purrai.runtime import Runtime
    from test_profile_trust import name_rule
    from test_ad_filters import named
    _, db, tg, svc = env
    await name_rule(db)
    await svc.message(named(1, "正文", "推广"))
    await drain(svc)
    tg.calls.clear()
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "admin_submission_notice": submission}
    runtime = Runtime(db, tg, svc)
    incoming = message(901, f"{command} 42 管理员核实", 1)
    try:
        assert await runtime.admin.message(incoming)
        assert await runtime.admin.message(incoming)
        await drain(svc)
        sent = [data for method, data in tg.calls if method == "send_message"]
        assert len(sent) == 1 + submission
        assert all(data["chat_id"] == -1001 for data in sent)
        assert any("已完成" in data["text"] for data in sent)
        assert sum("已提交" in data["text"] for data in sent) == submission
        async with db.transaction() as session:
            cleanups = (await session.scalars(select(Action).where(Action.dedupe_key.like("cleanup:%")))).all()
            receipts = (await session.scalars(select(Action).where(Action.kind == "admin_notify"))).all()
            sent_ids = {row.payload["delivery"]["result"]["message_id"] for row in receipts}
            assert len(sent_ids) == len(sent)
            assert {row.payload["message_id"] for row in cleanups} == {901, *sent_ids}
            deadline = max(row.available_at for row in cleanups)
            assert all(29 <= row.available_at - row.created_at <= 32 for row in cleanups)
            (await session.get(Group, -1001)).enabled = False
        restarted = Service(db, tg, env[0])
        monkeypatch.setattr("purrai.actions.now", lambda: deadline + 1)
        while await restarted.run_one_cleanup():
            pass
        deleted = {data["message_id"] for method, data in tg.calls if method == "delete_message"}
        assert {row.payload["message_id"] for row in cleanups} <= deleted
    finally:
        await runtime.stop()


@pytest.mark.parametrize("failure", ["network", "permissions", "bad_request"])
@pytest.mark.parametrize("cleanup", ["command", "notice"])
async def test_cleanup_survives_more_than_eight_failures_and_recovers_after_restart(env, monkeypatch, failure, cleanup):
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
    from aiogram.methods import DeleteMessage
    from purrai.action_queue import enqueue
    _, db, tg, svc = env
    async with db.transaction() as session:
        task = await enqueue(session, "delete", f"cleanup:{cleanup}:retry:test", -1001,
            payload={"chat_id": -1001, "message_id": 800})
    original = tg.call
    remaining = 9
    async def flaky(method, **kwargs):
        nonlocal remaining
        if method == "delete_message" and remaining:
            remaining -= 1
            if failure == "network":
                raise ConnectionError("offline")
            cls = TelegramForbiddenError if failure == "permissions" else TelegramBadRequest
            raise cls(method=DeleteMessage(**kwargs), message="not enough rights to delete messages")
        return await original(method, **kwargs)
    monkeypatch.setattr(tg, "call", flaky)
    for _ in range(9):
        async with db.transaction() as session:
            row = await session.get(Action, task.id)
            row.available_at = 0
        assert await svc.run_one_cleanup()
    async with db.transaction() as session:
        row = await session.get(Action, task.id)
        assert row.state == "pending" and row.attempts == 9
        row.available_at = 0
    assert await Service(db, tg, env[0]).run_one_cleanup()
    async with db.sessions() as session:
        assert (await session.get(Action, task.id)).state == "succeeded"
    assert [data["message_id"] for method, data in tg.calls if method == "delete_message"] == [800]


async def test_spam_notice_uses_name_and_expires_at_30_seconds_even_if_group_paused(env, monkeypatch):
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "repeat_count": 3, "similar_enabled": False, "burst_enabled": False}
    for identifier in (1, 2, 3):
        await svc.message(message(identifier, "签到", **{"from": {"id": 42, "first_name": "晏王爷", "username": "yanchenha"}}))
    await drain(svc)
    sent = [kw for method, kw in tg.calls if method == "send_message"]
    assert sent[-1]["text"] == "用户晏王爷：重复刷屏，已临时禁言 30 分钟。"
    async with db.transaction() as session:
        notice = await session.scalar(select(Action).where(Action.kind == "warn"))
        deletion = await session.scalar(select(Action).where(Action.dedupe_key == "cleanup:notice:" + notice.id))
        deadline, identifier = deletion.available_at, deletion.payload["message_id"]
        assert 29 <= deadline - notice.updated_at <= 31
        group = await session.get(Group, -1001)
        group.enabled = False
    restarted = Service(db, tg, env[0])
    monkeypatch.setattr("purrai.actions.now", lambda: deadline - 0.1)
    assert not await restarted.run_one_cleanup()
    monkeypatch.setattr("purrai.actions.now", lambda: deadline + 0.1)
    assert await restarted.run_one_cleanup()
    assert any(method == "delete_message" and kw["message_id"] == identifier for method, kw in tg.calls)


@pytest.mark.parametrize("state", ["succeeded", "cancelled"])
async def test_legacy_confirmed_notice_receipt_recovers_cleanup_without_resending(env, state):
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Action(id="old-notice", kind="warn", group_id=-1001, user_id=42, dedupe_key="old-notice",
            state=state, updated_at=now() - 500,
            payload={"text": "旧系统提示", "delivery": {"state": "confirmed", "method": "send_message", "result": {"message_id": 987}}}))
    assert await NoticeCleanup(db).reconcile() == 1
    assert await NoticeCleanup(db).reconcile() == 0
    assert await svc.run_one_cleanup()
    assert [kw["message_id"] for method, kw in tg.calls if method == "delete_message"] == [987]
    assert not any(method == "send_message" for method, _ in tg.calls)


async def test_cancelled_during_group_notice_send_still_deletes_confirmed_message(env, monkeypatch):
    from purrai.action_queue import enqueue
    from purrai.models import Member

    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, present=True))
        action = await enqueue(session, "warn", "race-notice", -1001, 42, {"text": "群内处置结果"})
    call = tg.call

    async def update_member_during_send(method, **kwargs):
        result = await call(method, **kwargs)
        if method == "send_message":
            async with db.transaction() as session:
                member = await session.get(Member, (-1001, 42))
                member.present = False
        return result

    monkeypatch.setattr(tg, "call", update_member_during_send)
    await drain(svc)
    async with db.sessions() as session:
        notice = await session.get(Action, action.id)
        assert notice.state == "cancelled"
        identifier = notice.payload["delivery"]["result"]["message_id"]
        cleanup = await session.scalar(select(Action).where(Action.dedupe_key == "cleanup:notice:" + notice.id))
        assert cleanup.payload["message_id"] == identifier
        assert cleanup.available_at == notice.updated_at + 30
    assert await NoticeCleanup(db).reconcile() == 0
    monkeypatch.setattr("purrai.actions.now", lambda: cleanup.available_at + 1)
    assert await svc.run_one_cleanup()
    assert [data["message_id"] for method, data in tg.calls if method == "delete_message"] == [identifier]
    assert len([1 for method, _ in tg.calls if method == "send_message"]) == 1


async def test_disabling_notices_suppresses_previously_queued_ai_warning(env, client):
    from purrai.action_queue import enqueue
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "ai_ad_mute_notice": True}
        notice = await enqueue(session, "warn", "ai-ad-notice:earlier-mute", -1001, 42, {"text": "AI广告禁言通知"})
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {"ai_ad_mute_notice": False}},
                                  headers={"Idempotency-Key": "disable-ai-mute-notices"})
    assert response.status_code == 200, response.text
    await drain(svc)
    async with db.sessions() as session:
        assert (await session.get(Action, notice.id)).state == "cancelled"
    assert not [name for name, _ in tg.calls if name == "send_message"]


@pytest.mark.parametrize("changed_during", ["get_chat_member", "delete_message"])
async def test_member_change_during_message_deletion_rechecks_or_records_confirmed_effect(env, monkeypatch, changed_during):
    from purrai.action_queue import enqueue
    from purrai.models import Member
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, present=True))
        action = await enqueue(session, "delete", "delete:fixed-message", -1001, 42, {"message_id": 987})
    original = tg.call
    changed = False

    async def change_member(method, **kwargs):
        nonlocal changed
        result = await original(method, **kwargs)
        if method == changed_during and not changed:
            changed = True
            async with db.transaction() as session:
                member = await session.get(Member, (-1001, 42))
                member.present = False
        return result

    monkeypatch.setattr(tg, "call", change_member)
    await svc.run_one_action()
    if changed_during == "get_chat_member":
        async with db.transaction() as session:
            queued = await session.get(Action, action.id)
            assert queued.state == "pending"
            queued.available_at = 0
        await svc.run_one_action()
    async with db.sessions() as session:
        assert (await session.get(Action, action.id)).state == "succeeded"
    assert changed
    assert [kw["message_id"] for method, kw in tg.calls if method == "delete_message"] == [987]
