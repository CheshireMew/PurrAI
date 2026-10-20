import asyncio
import time

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import RestrictChatMember
from sqlalchemy import select

from conftest import drain, join
from purrai import captcha
from purrai.models import Action, Verification
from purrai.telegram import ALL_PERMISSIONS
from test_verification import enter_code, legacy_challenge, fixed_challenge, latest


async def test_duplicate_callback_is_applied_once_even_when_ack_lost(env, monkeypatch):
    verification = await legacy_challenge(env, monkeypatch)
    _, database, telegram, service = env
    telegram.fail["answer_callback_query"] = 1
    query = {"id": "same-event", "from": {"id": 42}, "data": f"v:{verification.id}:{verification.nonce}:1"}
    await service.callback(query)
    await service.callback(query)
    assert (await latest(database)).input_digits == "1"


async def test_preserves_non_sending_permissions_and_previous_duration(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    until = int(time.time() + 1200)
    previous = {**dict.fromkeys(ALL_PERMISSIONS, True), "can_send_photos": False}
    telegram.members[(-1001, 42)] = {
        "status": "restricted",
        "is_member": True,
        "until_date": until,
        **previous,
    }
    event = join(new="restricted")
    event["new_chat_member"].update(is_member=True, until_date=until, **previous)
    await service.member_event(event)
    await drain(service)
    assert telegram.members[(-1001, 42)]["can_invite_users"] is True
    assert telegram.members[(-1001, 42)]["can_send_messages"] is False
    verification = await latest(database)
    await enter_code(service, verification, "123456")
    await drain(service)
    assert telegram.members[(-1001, 42)]["can_invite_users"] is True
    assert telegram.members[(-1001, 42)]["can_send_photos"] is False
    assert telegram.members[(-1001, 42)]["until_date"] == until


async def test_release_response_lost_after_telegram_success_is_reconciled(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await enter_code(service, verification, "123456")
    original = telegram.call
    lost = False

    async def response_lost(method, **kwargs):
        nonlocal lost
        response = await original(method, **kwargs)
        if method == "restrict_chat_member" and not lost:
            lost = True
            raise ConnectionError("response was lost")
        return response

    monkeypatch.setattr(telegram, "call", response_lost)
    await service.run_one_action()
    assert (await latest(database)).state == "releasing"
    async with database.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "release"))
        action.available_at = 0
    await drain(service)
    assert (await latest(database)).state == "passed"


async def test_rate_limit_uses_telegram_retry_after(env, monkeypatch):
    _, database, telegram, service = env
    await service.member_event(join())
    original = telegram.call

    async def limited(method, **kwargs):
        if method == "restrict_chat_member":
            raise TelegramRetryAfter(
                method=RestrictChatMember(chat_id=-1001, user_id=42, permissions={}),
                message="rate limited",
                retry_after=17,
            )
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", limited)
    await service.run_one_action()
    async with database.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "verify_mute"))
        assert action.state == "pending"
        assert 16 < action.available_at - time.time() <= 17


async def test_timeout_and_correct_submit_cannot_both_apply(env, monkeypatch):
    verification = await legacy_challenge(env, monkeypatch)
    _, database, telegram, service = env
    for digit in "123456":
        await service.callback(
            {
                "id": f"key-{digit}",
                "from": {"id": 42},
                "data": f"v:{verification.id}:{verification.nonce}:{digit}",
            }
        )
    async with database.transaction() as session:
        row = await session.get(Verification, verification.id)
        row.deadline = time.time() - 1
    await asyncio.gather(
        service.scan_timeouts(),
        service.callback(
            {"id": "late-submit", "from": {"id": 42}, "data": f"v:{verification.id}:{verification.nonce}:ok"}
        ),
    )
    await drain(service)
    assert (await latest(database)).state == "failed"
    assert not any(
        method == "restrict_chat_member" and kwargs["permissions"]["can_send_messages"]
        for method, kwargs in telegram.calls
    )
