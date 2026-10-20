import time

import pytest
from sqlalchemy import select

from conftest import drain, join
from purrai import captcha
from purrai.management import ManagementError
from purrai.models import Action, Group, Member, Verification
from purrai.runtime import Runtime
from purrai.schemas import ActionInput
from test_bot_admin import click, deliver, open_group
from test_verification import enter_code, latest


def request(date=None, user_id=42):
    return {
        "chat": {"id": -1001, "type": "supergroup", "title": "测试群"},
        "from": {"id": user_id, "is_bot": False, "first_name": "申请者"},
        "date": int(date or time.time()),
        "user_chat_id": user_id + 10000,
    }


async def prepare(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    telegram.members[(-1001, 42)] = {"status": "left"}
    async with database.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "join_requests": True}
    return Runtime(database, telegram, service)


async def test_request_is_verified_in_private_then_approved_without_group_mute(env, monkeypatch):
    runtime = await prepare(env, monkeypatch)
    _, database, telegram, service = env
    await deliver(runtime, {"chat_join_request": request()})
    await drain(service)
    verification = await latest(database)
    assert verification.flow == "join_request" and verification.state == "pending"
    assert any(method == "send_photo" and data["chat_id"] == 10042 for method, data in telegram.calls)
    assert not any(
        method in ("restrict_chat_member", "approve_chat_join_request") for method, _ in telegram.calls
    )
    await enter_code(service, verification, "123456")
    await drain(service)
    assert (await latest(database)).state == "passed"
    async with database.sessions() as session:
        assert not (await session.get(Member, (-1001, 42))).join_observed
    assert len([1 for method, _ in telegram.calls if method == "approve_chat_join_request"]) == 1
    assert any(method == "delete_message" and data["chat_id"] == 10042 for method, data in telegram.calls)
    await service.member_event(join(actor=telegram.user_id), event_id=99999)
    await drain(service)
    assert len([1 for method, _ in telegram.calls if method == "send_photo"]) == 1
    async with database.sessions() as session:
        assert (await session.get(Member, (-1001, 42))).present
        assert (await session.get(Member, (-1001, 42))).join_observed
        assert len((await session.scalars(select(Verification))).all()) == 1


async def test_duplicate_request_and_other_person_callback_cannot_approve(env, monkeypatch):
    await prepare(env, monkeypatch)
    _, database, telegram, service = env
    incoming = request()
    await service.join_request(incoming, event_id=1)
    await service.join_request(incoming, event_id=1)
    await drain(service)
    verification = await latest(database)
    await enter_code(service, verification, "123456", user_id=43)
    await drain(service)
    assert not any(method == "approve_chat_join_request" for method, _ in telegram.calls)
    async with database.sessions() as session:
        assert len((await session.scalars(select(Verification))).all()) == 1
        assert (await session.get(Verification, verification.id)).input_digits == ""


async def test_request_timeout_declines_and_enforces_local_retry_interval(env, monkeypatch):
    await prepare(env, monkeypatch)
    _, database, telegram, service = env
    await service.join_request(request(), event_id=1)
    await drain(service)
    old = await latest(database)
    async with database.transaction() as session:
        (await session.get(Verification, old.id)).deadline = 0
    await service.scan_timeouts()
    await drain(service)
    assert (await latest(database)).state == "failed"
    assert any(method == "decline_chat_join_request" for method, _ in telegram.calls)
    assert not any(method in ("restrict_chat_member", "ban_chat_member") for method, _ in telegram.calls)
    await enter_code(service, old, "123456")
    await drain(service)
    assert not any(method == "approve_chat_join_request" for method, _ in telegram.calls)
    await service.join_request(request(date=time.time() + 1), event_id=2)
    await drain(service)
    assert len([1 for method, _ in telegram.calls if method == "decline_chat_join_request"]) == 2


async def test_external_approval_or_direct_entry_falls_back_to_group_verification(env, monkeypatch):
    await prepare(env, monkeypatch)
    _, database, telegram, service = env
    await service.join_request(request(), event_id=1)
    await drain(service)
    first = await latest(database)
    telegram.members[(-1001, 42)] = {"status": "member"}
    await service.member_event(join(actor=123), event_id=2)
    await drain(service)
    async with database.sessions() as session:
        assert (await session.get(Verification, first.id)).state == "superseded"
    current = await latest(database)
    assert current.flow == "joined" and current.state == "pending"
    assert any(method == "send_photo" and data["chat_id"] == -1001 for method, data in telegram.calls)


async def test_manual_release_and_restart_resume_request_approval(env, monkeypatch):
    runtime = await prepare(env, monkeypatch)
    _, database, telegram, service = env
    await service.join_request(request(), event_id=1)
    await drain(service)
    verification = await latest(database)
    await runtime.management.verification_action(
        1,
        verification.id,
        ActionInput(action="release", reason="人工核实", request_id="manual-request-release"),
    )
    runtime = Runtime(database, telegram, service)
    await drain(runtime.service)
    assert (await latest(database)).state == "passed"
    assert any(method == "approve_chat_join_request" for method, _ in telegram.calls)


async def test_reissue_never_extends_past_private_contact_window(env, monkeypatch):
    runtime = await prepare(env, monkeypatch)
    _, database, _, service = env
    await service.join_request(request(), event_id=1)
    await drain(service)
    verification = await latest(database)
    async with database.transaction() as session:
        (await session.get(Verification, verification.id)).request_date = time.time() - 250
    with pytest.raises(ManagementError, match="重新申请"):
        await runtime.management.verification_action(
            1,
            verification.id,
            ActionInput(action="reissue", reason="重新验证", request_id="reissue-expired-request"),
        )


async def test_join_request_toggle_requires_invite_permission_and_link_is_durable(env, monkeypatch):
    runtime = await prepare(env, monkeypatch)
    _, database, telegram, service = env
    original = telegram.call

    async def missing(method, **values):
        response = await original(method, **values)
        if method == "get_chat_member" and values["user_id"] == telegram.user_id:
            response = {**response, "can_invite_users": False}
        return response

    monkeypatch.setattr(telegram, "call", missing)
    with pytest.raises(ManagementError, match="can_invite_users"):
        await runtime.management.setting(1, -1001, "join_requests", True, "enable-join-request")
    monkeypatch.setattr(telegram, "call", original)
    await open_group(runtime, telegram)
    query = await click(runtime, telegram, "生成申请入群链接")
    await drain(service)
    await deliver(runtime, {"callback_query": query})
    await drain(service)
    assert len([1 for method, _ in telegram.calls if method == "create_chat_invite_link"]) == 1
    assert any(
        method == "send_message" and "https://t.me/+offline-test" in values["text"]
        for method, values in telegram.calls
    )


async def test_request_approval_response_lost_is_reconciled_without_second_approval(env, monkeypatch):
    await prepare(env, monkeypatch)
    _, database, telegram, service = env
    await service.join_request(request(), event_id=1)
    await drain(service)
    verification = await latest(database)
    await enter_code(service, verification, "123456")
    original = telegram.call
    lost = False

    async def response_lost(method, **values):
        nonlocal lost
        response = await original(method, **values)
        if method == "approve_chat_join_request" and not lost:
            lost = True
            raise ConnectionError("lost")
        return response

    monkeypatch.setattr(telegram, "call", response_lost)
    await drain(service)
    assert (await latest(database)).state == "releasing"
    async with database.transaction() as session:
        for action in (await session.scalars(select(Action).where(Action.state == "pending"))).all():
            action.available_at = 0
    await drain(service)
    assert (await latest(database)).state == "passed"
    assert len([1 for method, _ in telegram.calls if method == "approve_chat_join_request"]) == 1


async def test_disabling_new_requests_does_not_abandon_an_existing_captcha(env, monkeypatch):
    await prepare(env, monkeypatch)
    _, database, telegram, service = env
    await service.join_request(request(), event_id=1)
    await drain(service)
    verification = await latest(database)
    async with database.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "join_requests": False}
    await enter_code(service, verification, "123456")
    await drain(service)
    assert (await latest(database)).state == "passed"
    assert any(method == "approve_chat_join_request" for method, _ in telegram.calls)


async def test_declined_request_cooldown_can_be_cleared_by_admin(env, monkeypatch):
    runtime = await prepare(env, monkeypatch)
    _, database, _, service = env
    await service.join_request(request(), event_id=1)
    await drain(service)
    verification = await latest(database)
    async with database.transaction() as session:
        (await session.get(Verification, verification.id)).deadline = 0
    await service.scan_timeouts()
    await drain(service)
    await runtime.management.verification_action(
        1,
        verification.id,
        ActionInput(action="unban", reason="允许重新申请", request_id="unblock-request-cooldown"),
    )
    await drain(service)
    async with database.sessions() as session:
        assert (await session.get(Member, (-1001, 42))).request_until == 0
