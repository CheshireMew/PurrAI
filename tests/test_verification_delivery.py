import pytest
from sqlalchemy import select

from conftest import drain, join
from purrai.management import Management
from purrai.models import Action, Group, Member, Verification, now
from purrai.schemas import ActionInput
from test_verification import fixed_challenge


async def test_initial_mute_checks_member_once_before_restriction(env):
    _, _, telegram, service = env
    await service.member_event(join())
    await service.run_one_action(include_progress=False)
    assert [method for method, _ in telegram.calls] == [
        "get_chat_member", "get_chat", "restrict_chat_member",
    ]


async def test_restricted_join_uses_current_permissions_without_group_lookup(env):
    _, database, telegram, service = env
    telegram.members[(-1001, 42)] = {
        "status": "restricted", "is_member": True, "until_date": 0,
        **telegram.defaults, "can_send_photos": False,
    }
    await service.member_event(join())
    await service.run_one_action(include_progress=False)
    assert [method for method, _ in telegram.calls] == ["get_chat_member", "restrict_chat_member"]
    async with database.sessions() as session:
        verification = await session.scalar(select(Verification))
        assert verification.previous_permissions["can_send_messages"] is True
        assert verification.previous_permissions["can_send_photos"] is False


async def test_reissue_reuses_confirmed_mute_without_restricting_again(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, service = env
    telegram.calls.clear()
    await Management(database, telegram, service, settings).verification_action(
        1, verification.id, ActionInput(action="reissue", reason="重出题", request_id="delivery-reissue"),
    )
    await drain(service)
    assert not any(method in ("restrict_chat_member", "get_chat") for method, _ in telegram.calls)
    assert sum(method == "send_photo" for method, _ in telegram.calls) == 1


@pytest.mark.parametrize("lost", [False, True])
async def test_photo_delivery_has_separate_timings_and_longer_receipt_timeout(env, monkeypatch, caplog, lost):
    _, database, telegram, _ = env
    if lost:
        telegram.fail["send_photo"] = 1
    clock = iter([0, 0.01, 0.01, 5.01])
    monkeypatch.setattr("purrai.verification_actions.perf_counter", lambda: next(clock))
    verification = await fixed_challenge(env, monkeypatch)
    async with database.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "challenge"))
        assert action.payload["challenge_delivery"] == ("uncertain" if lost else "confirmed")
        timing = action.payload["timing"]
        assert timing["queue_seconds"] >= 0 and timing["preparation_seconds"] >= 0
        assert timing["image_seconds"] == 0.01 and timing["send_seconds"] == 5
        assert timing["send_finished_at"] >= timing["send_started_at"]
        assert (await session.get(Member, (-1001, 42))).present
    photo = next(args for method, args in telegram.calls if method == "send_photo")
    assert photo["request_timeout"] == 30
    assert "send=5.000s" in caplog.text and action.id in caplog.text
    assert verification.state == "pending"


async def test_admin_with_stale_conflict_still_exempts_before_initial_mute(env):
    _, database, telegram, service = env
    await service.member_event(join())
    async with database.transaction() as session:
        (await session.get(Member, (-1001, 42))).restriction_conflict = True
        assert (await session.get(Group, -1001)).enabled
    telegram.members[(-1001, 42)] = {"status": "administrator"}
    await drain(service)
    assert not any(method == "restrict_chat_member" for method, _ in telegram.calls)
    async with database.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "passed"


async def test_unconfirmed_delivery_retries_past_deadline_and_attempt_limit_then_can_pass(env, monkeypatch):
    from purrai.service import Service
    from test_verification import enter_code, latest

    settings, database, telegram, service = env
    telegram.fail["send_photo"] = 10
    verification = await fixed_challenge(env, monkeypatch)
    original_hash, original_choices = verification.answer_hash, verification.choices
    for _ in range(9):
        async with database.transaction() as session:
            (await session.get(Verification, verification.id)).deadline = 0
            task = await session.scalar(select(Action).where(Action.kind == "challenge"))
            task.available_at = 0
        await service.scan_timeouts()
        await drain(service)
    async with database.sessions() as session:
        task = await session.scalar(select(Action).where(Action.kind == "challenge"))
        row = await session.get(Verification, verification.id)
        assert task.attempts == 10 and task.state == "pending"
        assert row.state == "pending" and row.message_id is None and row.failure_action == ""
        assert row.answer_hash == original_hash and row.choices == original_choices
        assert not await session.scalar(select(Action).where(Action.kind == "verification_failure"))
    async with database.transaction() as session:
        (await session.get(Action, task.id)).available_at = 0
    service = Service(database, telegram, settings)
    before = now()
    await drain(service)
    verification = await latest(database)
    assert verification.message_id and verification.deadline >= before + 180
    photos = [args for method, args in telegram.calls if method == "send_photo"]
    assert len(photos) == 11 and all(p["photo"] == photos[0]["photo"] for p in photos)
    assert all(p["reply_markup"] == photos[0]["reply_markup"] for p in photos)
    await enter_code(service, verification, "123456")
    await drain(service)
    assert (await latest(database)).state == "passed"
    assert telegram.members[(-1001, 42)]["can_send_messages"] is True


async def test_expired_unsent_queue_is_delivered_before_any_timeout_penalty(env, monkeypatch):
    from purrai import captcha
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    await service.member_event(join())
    async with database.transaction() as session:
        (await session.scalar(select(Verification))).deadline = 0
    await service.scan_timeouts()
    await drain(service)
    async with database.sessions() as session:
        row = await session.scalar(select(Verification))
        assert row.state == "pending" and row.message_id and row.deadline > now()
        assert not await session.scalar(select(Action).where(Action.kind == "verification_failure"))
    assert sum(method == "send_photo" for method, _ in telegram.calls) == 1


async def test_explicit_send_rejection_alerts_without_punishing_member(env, monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendPhoto
    _, database, telegram, service = env
    original = telegram.call

    async def rejected(method, **kwargs):
        if method == "send_photo":
            raise TelegramForbiddenError(method=SendPhoto(chat_id=-1001, photo="test"), message="forbidden")
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", rejected)
    verification = await fixed_challenge(env, monkeypatch)
    async with database.transaction() as session:
        (await session.get(Verification, verification.id)).deadline = 0
    await service.scan_timeouts()
    await drain(service)
    async with database.sessions() as session:
        row = await session.get(Verification, verification.id)
        assert row.state == "pending" and not row.failure_action
        task = await session.scalar(select(Action).where(Action.kind == "challenge"))
        assert task.state == "failed" and task.payload["challenge_delivery"] == "rejected"
        assert not await session.scalar(select(Action).where(Action.kind == "verification_failure"))


@pytest.mark.parametrize("kind", ["advertisement_ban", "rule_mute", "manual_mute", "manual_ban"])
async def test_other_administrators_restriction_is_cancelled_and_not_alerted(env, kind):
    from purrai.action_queue import enqueue
    from purrai.runtime_alerts import RuntimeAlerts
    settings, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, restriction_conflict=True))
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "inline_buttons_ban": True}
        task = await enqueue(session, kind, "external-admin-test", -1001, 42,
            {"ad_evidence": {"message": {"reply_markup": {"inline_keyboard": [
                [{"text": "test", "url": "https://example.org"}]]}}}})
    telegram.members[(-1001, 42)] = {"status": "restricted", "is_member": True,
        "until_date": 0, **telegram.defaults, "can_send_messages": False}
    await drain(service)
    await RuntimeAlerts(database, settings).cycle()
    async with database.sessions() as session:
        task = await session.get(Action, task.id)
        assert task.state == "cancelled"
        assert task.payload["outcome"] == "external_restriction_preserved"
        assert not await session.scalar(select(Action).where(Action.kind == "owner_alert"))
    assert not any(method in ("restrict_chat_member", "ban_chat_member") for method, _ in telegram.calls)


async def test_callback_during_retry_recovers_original_and_cleans_late_duplicate(env, monkeypatch):
    import asyncio
    from test_captcha_cleanup import lost_challenge_receipt
    from test_captcha_choices import choice_query
    from test_verification import latest

    _, database, telegram, service = env
    original_call = telegram.call
    verification, first_message_id = await lost_challenge_receipt(env, monkeypatch)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delayed_retry(method, **kwargs):
        if method == "send_photo":
            entered.set()
            await finish.wait()
        return await original_call(method, **kwargs)

    monkeypatch.setattr(telegram, "call", delayed_retry)
    async with database.transaction() as session:
        (await session.scalar(select(Action).where(Action.kind == "challenge"))).available_at = 0
        (await session.get(Verification, verification.id)).deadline = 0
    running = asyncio.create_task(service.run_one_action())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await service.scan_timeouts()
        query = choice_query(verification)
        query["message"]["message_id"] = first_message_id
        assert "答案正确" in await service.callback(query)
    finally:
        finish.set()
        await asyncio.wait_for(running, 3)
    await drain(service)
    assert (await latest(database)).state == "passed"
    deleted = {data["message_id"] for method, data in telegram.calls if method == "delete_message"}
    assert first_message_id in deleted and first_message_id + 1 in deleted
    async with database.sessions() as session:
        assert not await session.scalar(select(Action).where(Action.kind == "verification_failure"))


async def test_legacy_queued_timeout_without_delivery_is_cancelled_and_resent(env, monkeypatch):
    from purrai.action_queue import enqueue
    from test_verification import latest

    _, database, telegram, service = env
    telegram.fail["send_photo"] = 1
    verification = await fixed_challenge(env, monkeypatch)
    async with database.transaction() as session:
        (await session.scalar(select(Action).where(Action.kind == "challenge"))).state = "failed"
        v = await session.get(Verification, verification.id)
        v.state, v.nonce = "failing", "legacy-failure-nonce"
        penalty = await enqueue(session, "verification_failure", "legacy-timeout", -1001, 42,
            {"verification_id": v.id}, 0, reason="验证超时")
    await drain(service)
    async with database.sessions() as session:
        assert (await session.get(Action, penalty.id)).state == "cancelled"
    current = await latest(database)
    assert current.state == "pending" and current.message_id and current.deadline > now()
    assert not current.failure_action
