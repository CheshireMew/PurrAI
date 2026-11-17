import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, Group, now
from purrai.notice_cleanup import NoticeCleanup
from purrai.service import Service


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
