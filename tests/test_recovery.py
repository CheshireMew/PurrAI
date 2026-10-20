import time

from sqlalchemy import select

from conftest import drain, join, message
from purrai import captcha
from purrai.models import Inbox, Meta, Rule, Verification, Violation
from purrai.runtime import Runtime
from purrai.service import enqueue


async def test_inbox_persists_before_offset_and_strips_processed_message(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    payload = {"update_id": 12, "message": message(99, "普通消息")}
    payload["message"]["from"].update(is_bot=False, first_name="测试")
    await runtime.persist([payload])
    await runtime.persist([payload])
    async with database.sessions() as session:
        assert (await session.get(Meta, "poll_offset")).value == "13"
        assert len((await session.scalars(select(Inbox))).all()) == 1
    await runtime.process_one()
    async with database.sessions() as session:
        row = await session.get(Inbox, 12)
        assert row.state == "done" and row.payload == {"update_id": 12}


async def test_rejoin_same_second_starts_new_challenge(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, _, service = env
    date = int(time.time())
    await service.member_event(join(date=date), event_id=1)
    await service.member_event(join(date=date, old="member", new="left"), event_id=2)
    await service.member_event(join(date=date), event_id=3)
    await drain(service)
    # A retry of the old event must not replace the newest membership generation.
    await service.member_event(join(date=date), event_id=1)
    async with database.sessions() as session:
        rows = (await session.scalars(select(Verification))).all()
        assert len(rows) == 2
        assert sorted(r.state for r in rows) == ["left", "pending"]


async def test_changed_manual_decision_cancels_old_ban(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    await service.member_event(join())
    await drain(service)
    async with database.transaction() as session:
        verification = await session.scalar(select(Verification))
        verification.state = "removing"
        await enqueue(session, "ban", "old-ban", -1001, 42, {"verification_id": verification.id}, 0)
        verification.state = "releasing"
        await enqueue(session, "release", "new-release", -1001, 42, {"verification_id": verification.id}, 0)
    await drain(service)
    assert not any(method == "ban_chat_member" for method, _ in telegram.calls)
    async with database.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "passed"


async def test_revoking_warning_before_pending_mute_prevents_penalty(env):
    _, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="广告"))
    for number in range(3):
        await service.message(message(number, "广告"))
    async with database.transaction() as session:
        violation = await session.scalar(select(Violation))
        violation.warning_active = False
    await drain(service)
    assert not any(method == "restrict_chat_member" for method, _ in telegram.calls)


async def test_manual_release_before_mute_does_not_leave_stuck_task(env):
    _, database, telegram, service = env
    await service.member_event(join())
    async with database.transaction() as session:
        verification = await session.scalar(select(Verification))
        verification.state = "releasing"
        await enqueue(session, "release", "early-release", -1001, 42, {"verification_id": verification.id}, 0)
    await drain(service)
    async with database.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "passed"


async def test_temporarily_banned_member_can_be_unbanned_from_verification(env, client, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    await service.member_event(join())
    await drain(service)
    async with database.transaction() as session:
        verification = await session.scalar(select(Verification))
        identifier = verification.id
        verification.deadline = 0
    from purrai.management import Management
    from purrai.schemas import ActionInput

    await Management(database, telegram, service, env[0]).verification_action(
        1, identifier, ActionInput(action="ban", reason="管理员明确封禁", request_id="manual-legacy-ban")
    )
    await drain(service)
    response = await client.post(
        f"/api/verifications/{identifier}/actions",
        json={"action": "unban", "reason": "核实后允许重试", "request_id": "unban-request"},
    )
    assert response.status_code == 200
    await drain(service)
    assert telegram.members[(-1001, 42)]["status"] == "left"
    repeat = await client.post(
        f"/api/verifications/{identifier}/actions",
        json={"action": "unban", "reason": "核实后允许重试", "request_id": "unban-request"},
    )
    assert repeat.json()["id"] == response.json()["id"]


def test_process_lock_rejects_second_worker_and_releases(tmp_path):
    from purrai.process_lock import ProcessLock
    import pytest

    url = f"sqlite+aiosqlite:///{tmp_path / 'db'}"
    first, second = ProcessLock(url), ProcessLock(url)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="单 worker"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
