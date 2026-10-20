import time

import pytest
from sqlalchemy import select

from conftest import drain, join
from purrai.management import Management
from purrai.models import Action, Group, Member, Verification
from purrai.runtime import Runtime
from purrai.schemas import ActionInput
from purrai.service import Service
from test_bot_admin import click, markup, open_group
from test_join_requests import prepare, request
from test_verification import enter_code, fixed_challenge, latest


async def expire(database, service, verification):
    async with database.transaction() as session:
        (await session.get(Verification, verification.id)).deadline = time.time() - 1
    await service.scan_timeouts()


async def test_default_failed_verification_stays_muted_after_restart_and_can_be_released(
    env, monkeypatch, client
):
    verification = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, service = env
    await expire(database, service, verification)
    await drain(service)
    member = telegram.members[(-1001, 42)]
    assert member["status"] == "restricted" and member["is_member"] and member["until_date"] == 0
    assert not member["can_send_messages"]
    assert not any(method in ("ban_chat_member", "unban_chat_member") for method, _ in telegram.calls)
    await drain(Service(database, telegram, settings))
    assert (await latest(database)).failure_action == "permanent_mute"
    await enter_code(service, verification, "123456")
    assert (await latest(database)).state == "failed"
    telegram.defaults["can_send_photos"] = False
    response = await client.post(
        f"/api/verifications/{verification.id}/actions",
        json={"action": "release", "reason": "核实后人工放行", "request_id": "release-failed-permanent"},
    )
    assert response.status_code == 200
    await drain(service)
    assert (await latest(database)).state == "passed"
    assert telegram.members[(-1001, 42)]["can_send_messages"]
    assert not telegram.members[(-1001, 42)]["can_send_photos"]


async def test_admin_can_reissue_after_permanent_failure_and_old_buttons_stay_invalid(env, monkeypatch):
    original = await fixed_challenge(env, monkeypatch)
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    await expire(db, service, original)
    await drain(service)
    task = await management.member_action(1, -1001, 42, "reissue", "retry-permanent-captcha")
    assert task["kind"] == "verify_mute"
    await drain(service)
    new = await latest(db)
    assert new.state == "pending" and new.nonce != original.nonce
    await enter_code(service, original, "123456")
    assert (await latest(db)).state == "pending"
    await enter_code(service, new, "123456")
    await drain(service)
    assert (await latest(db)).state == "passed"


async def test_member_unmute_command_routes_failed_permanent_verification_through_release(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, db, tg, service = env
    await expire(db, service, verification)
    await drain(service)
    task = await Management(db, tg, service, settings).member_action(
        1, -1001, 42, "unmute", "unmute-failed-member"
    )
    assert task["kind"] == "release"
    await drain(service)
    assert (await latest(db)).state == "passed"


async def test_kick_removes_member_then_immediately_allows_rejoin(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_failure_action": "kick"}
    await expire(db, service, verification)
    await drain(service)
    assert tg.members[(-1001, 42)]["status"] == "left"
    assert (await latest(db)).failure_action == "kick"
    assert len([1 for method, _ in tg.calls if method == "ban_chat_member"]) == 1
    assert len([1 for method, _ in tg.calls if method == "unban_chat_member"]) == 1
    async with db.sessions() as session:
        assert (await session.get(Member, (-1001, 42))).owned_restriction is None
    tg.members[(-1001, 42)] = {"status": "member"}
    await service.member_event(join(date=time.time() + 1), event_id=20)
    await drain(service)
    assert (await latest(db)).state == "pending"


@pytest.mark.parametrize(
    "method,apply_first",
    [("ban_chat_member", True), ("unban_chat_member", False), ("unban_chat_member", True)],
)
async def test_kick_retry_recovers_lost_responses_without_duplicate_removal(
    env, monkeypatch, method, apply_first
):
    verification = await fixed_challenge(env, monkeypatch)
    settings, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_failure_action": "kick"}
    original = tg.call
    failed = False

    async def fail_once(name, **kwargs):
        nonlocal failed
        if name == method and not failed:
            failed = True
            if apply_first:
                await original(name, **kwargs)
            raise ConnectionError("response lost")
        return await original(name, **kwargs)

    monkeypatch.setattr(tg, "call", fail_once)
    await expire(db, service, verification)
    await service.run_one_action()
    row = await latest(db)
    assert row.state == "failing" and row.failure_action == ""
    async with db.transaction() as session:
        task = await session.scalar(select(Action).where(Action.kind == "verification_failure"))
        assert task.state == "pending"
        task.available_at = 0
    await drain(Service(db, tg, settings))
    assert (await latest(db)).failure_action == "kick"
    assert tg.members[(-1001, 42)]["status"] == "left"
    assert len([1 for name, _ in tg.calls if name == "ban_chat_member"]) == 1


async def test_failed_permanent_mute_is_not_reported_complete_before_telegram_succeeds(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, db, tg, service = env
    tg.fail["restrict_chat_member"] = 1
    await expire(db, service, verification)
    await service.run_one_action()
    assert (await latest(db)).state == "failing"
    async with db.transaction() as session:
        task = await session.scalar(select(Action).where(Action.kind == "verification_failure"))
        assert task.state == "pending"
        task.available_at = 0
    await drain(Service(db, tg, settings))
    assert (await latest(db)).state == "failed"


async def test_manual_release_cancels_queued_failure(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, db, tg, service = env
    await expire(db, service, verification)
    await Management(db, tg, service, settings).verification_action(
        1,
        verification.id,
        ActionInput(action="release", reason="优先人工放行", request_id="manual-before-failure"),
    )
    await drain(service)
    assert (await latest(db)).state == "passed"
    assert not any(name == "ban_chat_member" for name, _ in tg.calls)
    async with db.sessions() as session:
        task = await session.scalar(select(Action).where(Action.kind == "verification_failure"))
        assert task.state == "cancelled"


@pytest.mark.parametrize("choice", ["kick", "permanent_mute"])
async def test_join_request_failure_only_declines_for_both_choices(env, monkeypatch, choice):
    await prepare(env, monkeypatch)
    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_failure_action": choice}
    await service.join_request(request(), event_id=1)
    await drain(service)
    row = await latest(db)
    await expire(db, service, row)
    await drain(service)
    assert (await latest(db)).failure_action == "decline"
    assert not any(
        name in ("ban_chat_member", "restrict_chat_member", "approve_chat_join_request")
        for name, _ in tg.calls
    )
    assert any(name == "decline_chat_join_request" for name, _ in tg.calls)


async def test_bot_shows_both_failure_choices_and_saves_selected_policy(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, "验证设置")
    keyboard = str(markup(tg))
    assert "● 验证失败：永久禁言" in keyboard and "○ 验证失败：踢出群聊" in keyboard
    assert "验证失败封禁" not in keyboard
    await click(runtime, tg, "验证失败：踢出群聊")
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["verification_failure_action"] == "kick"
    assert "● 验证失败：踢出群聊" in str(markup(tg))
    await click(runtime, tg, "验证失败：永久禁言")
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["verification_failure_action"] == "permanent_mute"


async def test_api_rejects_invalid_failure_choice_and_keeps_other_settings(env, client):
    _, db, _, _ = env
    async with db.sessions() as session:
        settings = (await session.get(Group, -1001)).settings
    bad = await client.patch(
        "/api/groups/-1001/settings", json={"settings": {**settings, "verification_failure_action": "ban"}}
    )
    assert bad.status_code == 422
    good = await client.patch(
        "/api/groups/-1001/settings", json={"settings": {**settings, "verification_failure_action": "kick"}}
    )
    assert good.status_code == 200
    assert good.json()["settings"]["mute_seconds"] == 259200
    assert good.json()["settings"]["verification_failure_action"] == "kick"
