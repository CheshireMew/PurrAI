from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, Group, now
from purrai.notice_cleanup import NoticeCleanup
from purrai.service import Service


async def test_spam_notice_uses_name_and_expires_at_180_seconds_even_if_group_paused(env, monkeypatch):
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
        assert 179 <= deadline - notice.updated_at <= 181
        group = await session.get(Group, -1001)
        group.enabled = False
    restarted = Service(db, tg, env[0])
    monkeypatch.setattr("purrai.actions.now", lambda: deadline - 0.1)
    assert not await restarted.run_one_cleanup()
    monkeypatch.setattr("purrai.actions.now", lambda: deadline + 0.1)
    assert await restarted.run_one_cleanup()
    assert any(method == "delete_message" and kw["message_id"] == identifier for method, kw in tg.calls)


async def test_legacy_confirmed_notice_receipt_recovers_cleanup_without_resending(env):
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Action(id="old-notice", kind="warn", group_id=-1001, user_id=42, dedupe_key="old-notice",
            state="succeeded", updated_at=now() - 500,
            payload={"text": "旧系统提示", "delivery": {"state": "confirmed", "method": "send_message", "result": {"message_id": 987}}}))
    assert await NoticeCleanup(db).reconcile() == 1
    assert await NoticeCleanup(db).reconcile() == 0
    assert await svc.run_one_cleanup()
    assert [kw["message_id"] for method, kw in tg.calls if method == "delete_message"] == [987]
    assert not any(method == "send_message" for method, _ in tg.calls)
