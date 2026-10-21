import asyncio
import time

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import DeleteMessage, SendPhoto
from sqlalchemy import select

from conftest import drain, join
from purrai.models import Action, Group, Member, Verification
from purrai.runtime import Runtime
from purrai.service import Service
from test_failure_options import expire
from test_captcha_choices import choice_query
from test_join_requests import prepare, request
from test_verification import fixed_challenge


async def cleanup_task(database, verification):
    async with database.sessions() as session:
        return await session.scalar(select(Action).where(
            Action.dedupe_key == f"cleanup:{verification.id}:{verification.message_id}"
        ))


async def test_expiration_persists_prompt_deletion_before_penalty_executes(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await expire(database, service, verification)

    task = await cleanup_task(database, verification)
    assert task is not None and task.state == "pending"
    assert task.payload == {"message_id": verification.message_id, "chat_id": verification.group_id}
    assert not any(method == "delete_message" for method, _ in telegram.calls)


async def test_permanent_penalty_failure_does_not_leave_expired_prompt(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    async with database.transaction() as session:
        (await session.get(Member, (verification.group_id, verification.user_id))).restriction_conflict = True
    await expire(database, service, verification)
    await drain(service)

    async with database.sessions() as session:
        penalty = await session.scalar(select(Action).where(Action.kind == "verification_failure"))
        assert penalty.state == "failed" and "管理员" in penalty.error
        assert (await session.get(Verification, verification.id)).state == "failing"
    assert any(method == "delete_message" and data["message_id"] == verification.message_id
               for method, data in telegram.calls)


@pytest.mark.parametrize("policy,method", [("permanent_mute", "restrict_chat_member"), ("kick", "ban_chat_member")])
async def test_prompt_cleanup_runs_while_penalty_request_is_blocked(env, monkeypatch, policy, method):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    async with database.transaction() as session:
        group = await session.get(Group, verification.group_id)
        group.settings = {**group.settings, "verification_failure_action": policy}
    await expire(database, service, verification)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = telegram.call

    async def slow_penalty(name, **kwargs):
        if name == method:
            entered.set()
            await resume.wait()
        return await original(name, **kwargs)

    monkeypatch.setattr(telegram, "call", slow_penalty)
    pending = asyncio.create_task(service.run_one_action(include_progress=False, include_cleanup=False))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert await asyncio.wait_for(service.run_one_cleanup(), 3)
        assert not pending.done()
        assert (await cleanup_task(database, verification)).state == "succeeded"
        assert any(name == "delete_message" and data["message_id"] == verification.message_id
                   for name, data in telegram.calls)
    finally:
        resume.set()
        await asyncio.wait_for(pending, 3)


async def test_cleanup_is_not_cancelled_by_concurrent_member_changes(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await expire(database, service, verification)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = telegram.call

    async def slow_delete(name, **kwargs):
        if name == "delete_message":
            entered.set()
            await resume.wait()
        return await original(name, **kwargs)

    monkeypatch.setattr(telegram, "call", slow_delete)
    pending = asyncio.create_task(service.run_one_cleanup())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with database.transaction() as session:
            member = await session.get(Member, (verification.group_id, verification.user_id))
            member.present, member.generation, member.restriction_conflict = False, "new-membership", True
        resume.set()
        assert await asyncio.wait_for(pending, 3)
        assert (await cleanup_task(database, verification)).state == "succeeded"
    finally:
        resume.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_cleanup_retry_survives_restart_and_reconciliation_keeps_cooldown(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, service = env
    await expire(database, service, verification)
    telegram.fail["delete_message"] = 1
    assert await service.run_one_cleanup()
    task = await cleanup_task(database, verification)
    assert task.state == "pending" and task.attempts == 1 and task.available_at > time.time()

    restarted = Service(database, telegram, settings)
    assert await restarted.reconcile_verification_cleanup() == 0
    assert not await restarted.run_one_cleanup()
    async with database.transaction() as session:
        (await session.get(Action, task.id)).available_at = 0
    assert await restarted.run_one_cleanup()
    assert (await cleanup_task(database, verification)).state == "succeeded"
    assert await restarted.reconcile_verification_cleanup() == 0
    async with database.sessions() as session:
        assert (await session.get(Verification, verification.id)).state == "failing"


@pytest.mark.parametrize("state,expired", [
    ("failing", True), ("failed", True), ("passed", False),
    ("cancelled", False), ("releasing", True), ("pending", True),
])
async def test_restart_backfills_old_orphan_without_replaying_penalty(env, monkeypatch, state, expired):
    verification = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, _ = env
    async with database.transaction() as session:
        row = await session.get(Verification, verification.id)
        row.state, row.deadline = state, time.time() + (-1 if expired else 300)
        (await session.get(Group, verification.group_id)).approved = False
    calls_before = len(telegram.calls)
    restarted = Service(database, telegram, settings)
    assert await restarted.reconcile_verification_cleanup() == 1
    assert await restarted.reconcile_verification_cleanup() == 0
    assert await restarted.run_one_cleanup()
    assert (await cleanup_task(database, verification)).state == "succeeded"
    assert await restarted.reconcile_verification_cleanup() == 0
    assert [name for name, _ in telegram.calls[calls_before:]] == ["delete_message"]
    async with database.sessions() as session:
        assert (await session.get(Verification, verification.id)).state == state


async def test_backfill_keeps_unexpired_active_prompt(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, _, service = env
    assert verification.deadline > time.time()
    assert await service.reconcile_verification_cleanup() == 0
    assert await cleanup_task(database, verification) is None


async def test_backfill_is_bounded_and_deduplicated(env, monkeypatch):
    first = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await service.member_event(join(user_id=43))
    await drain(service)
    async with database.transaction() as session:
        rows = (await session.scalars(select(Verification))).all()
        assert len(rows) == 2
        message_ids = {row.message_id for row in rows}
        for row in rows:
            row.state = "failing"
    assert await service.reconcile_verification_cleanup(limit=1) == 1
    assert await service.reconcile_verification_cleanup(limit=1) == 1
    assert await service.reconcile_verification_cleanup(limit=1) == 0
    await drain(service)
    assert {data["message_id"] for name, data in telegram.calls if name == "delete_message"} == message_ids
    assert (await cleanup_task(database, first)).state == "succeeded"


async def test_private_request_prompt_is_cleaned_even_when_decline_fails(env, monkeypatch):
    await prepare(env, monkeypatch)
    _, database, telegram, service = env
    await service.join_request(request(), event_id=1)
    await drain(service)
    async with database.sessions() as session:
        verification = await session.scalar(select(Verification))
    telegram.fail["decline_chat_join_request"] = 1
    await expire(database, service, verification)
    await drain(service)
    assert (await cleanup_task(database, verification)).state == "succeeded"
    assert any(name == "delete_message" and data == {
        "chat_id": verification.prompt_chat_id, "message_id": verification.message_id,
    } for name, data in telegram.calls)
    async with database.sessions() as session:
        assert (await session.get(Verification, verification.id)).state == "failing"


async def test_cleanup_of_already_deleted_prompt_completes_without_requeue(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await expire(database, service, verification)
    original = telegram.call

    async def absent(name, **kwargs):
        if name == "delete_message":
            raise TelegramBadRequest(
                method=DeleteMessage(**kwargs), message="Bad Request: message to delete not found",
            )
        return await original(name, **kwargs)

    monkeypatch.setattr(telegram, "call", absent)
    assert await service.run_one_cleanup()
    task = await cleanup_task(database, verification)
    assert task.state == "succeeded" and task.payload["already_absent"]
    assert await service.reconcile_verification_cleanup() == 0


async def test_runtime_starts_cleanup_worker_and_recovers_orphan(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    async with database.transaction() as session:
        (await session.get(Verification, verification.id)).state = "failing"
    runtime = Runtime(database, telegram, service)
    idle_event, cleaned = asyncio.Event(), asyncio.Event()
    original = service.run_one_cleanup

    async def idle(*args, **kwargs):
        await idle_event.wait()

    async def observe_cleanup():
        result = await original()
        if result:
            cleaned.set()
        return result

    monkeypatch.setattr(service, "run_one_cleanup", observe_cleanup)
    for owner, method in [(runtime, "poll_loop"), (runtime, "inbox_loop"), (service, "action_loop"),
                          (service.progress, "loop"), (service.emoji_status, "loop"), (runtime.chat, "run")]:
        monkeypatch.setattr(owner, method, idle)
    runtime.start()
    try:
        assert any(task.get_name() == "verification-cleanup" for task in runtime.tasks)
        await asyncio.wait_for(cleaned.wait(), 3)
    finally:
        await runtime.stop()
    assert (await cleanup_task(database, verification)).state == "succeeded"
    assert any(name == "delete_message" and data["message_id"] == verification.message_id
               for name, data in telegram.calls)


async def lost_challenge_receipt(env, monkeypatch):
    telegram = env[2]
    original, sent = telegram.call, {}

    async def lose_receipt(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "send_photo":
            sent.update(result)
            raise TelegramNetworkError(method=SendPhoto(chat_id=-1001, photo="test"), message="Request timeout")
        return result

    monkeypatch.setattr(telegram, "call", lose_receipt)
    verification = await fixed_challenge(env, monkeypatch)
    return verification, sent["message_id"]


async def test_lost_send_receipt_does_not_retry_or_replace_the_question(env, monkeypatch):
    verification, _ = await lost_challenge_receipt(env, monkeypatch)
    settings, database, telegram, _ = env
    assert verification.state == "pending" and verification.message_id is None
    assert "未重复出题" in verification.error
    restarted = Service(database, telegram, settings)
    assert not await restarted.run_one_action()
    assert len([1 for method, _ in telegram.calls if method == "send_photo"]) == 1


async def test_callback_recovers_lost_receipt_and_completion_cleans_original_picture(env, monkeypatch):
    verification, message_id = await lost_challenge_receipt(env, monkeypatch)
    _, database, telegram, service = env
    query = choice_query(verification)
    query["message"]["message_id"] = message_id
    assert "答案正确" in await service.callback(query)
    await drain(service)
    async with database.sessions() as session:
        row = await session.get(Verification, verification.id)
        assert row.state == "passed" and row.message_id == message_id
        task = await session.scalar(select(Action).where(Action.kind == "challenge"))
        assert task.state == "succeeded" and task.payload["challenge_delivery"] == "recovered"
    assert any(method == "delete_message" and data["message_id"] == message_id for method, data in telegram.calls)
    assert len([1 for method, _ in telegram.calls if method == "send_photo"]) == 1


async def test_expired_unknown_picture_is_cleaned_when_its_buttons_reveal_message_id(env, monkeypatch):
    verification, message_id = await lost_challenge_receipt(env, monkeypatch)
    _, database, telegram, service = env
    query = choice_query(verification)
    query["message"]["message_id"] = message_id
    await expire(database, service, verification)
    await drain(service)
    assert "失效" in await service.callback(query)
    await drain(service)
    assert any(method == "delete_message" and data["message_id"] == message_id for method, data in telegram.calls)
    async with database.sessions() as session:
        assert (await session.get(Verification, verification.id)).state == "failed"


@pytest.mark.parametrize("origin", ["old_picture", "other_chat", "other_bot"])
async def test_stale_duplicate_callback_cleans_only_the_exact_bot_picture(env, monkeypatch, origin):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    query = choice_query(verification)
    query["message"]["message_id"] += 50
    query["message"]["from"] = {"id": telegram.user_id}
    if origin == "other_chat":
        query["message"]["chat"]["id"] = -1002
    elif origin == "other_bot":
        query["message"]["from"]["id"] = 555
    assert "失效" in await service.callback(query)
    await drain(service)
    deleted = [data["message_id"] for method, data in telegram.calls if method == "delete_message"]
    assert deleted == ([query["message"]["message_id"]] if origin == "old_picture" else [])
    async with database.sessions() as session:
        row = await session.get(Verification, verification.id)
        assert row.state == "pending" and row.attempts == 0 and row.message_id == verification.message_id


async def test_crash_during_picture_send_does_not_resend_after_restart(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, _ = env
    async with database.transaction() as session:
        row = await session.get(Verification, verification.id)
        row.state, row.message_id = "muting", None
        action = await session.scalar(select(Action).where(Action.kind == "challenge"))
        action.state = "pending"
        action.payload = {**action.payload, "challenge_delivery": "sending"}
    assert await Service(database, telegram, settings).run_one_action()
    assert len([1 for method, _ in telegram.calls if method == "send_photo"]) == 1
    async with database.sessions() as session:
        assert (await session.get(Verification, verification.id)).state == "pending"


async def test_explicit_picture_rate_limit_remains_retryable(env, monkeypatch):
    telegram = env[2]
    original, attempts = telegram.call, 0

    async def rate_limit_once(method, **kwargs):
        nonlocal attempts
        if method == "send_photo":
            attempts += 1
            if attempts == 1:
                raise TelegramRetryAfter(method=SendPhoto(chat_id=-1001, photo="test"), message="wait", retry_after=1)
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", rate_limit_once)
    verification = await fixed_challenge(env, monkeypatch)
    _, database, _, service = env
    async with database.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "challenge"))
        assert action.state == "pending" and action.payload["challenge_delivery"] == "rejected"
        action.available_at = 0
    assert await service.run_one_action()
    async with database.sessions() as session:
        row = await session.get(Verification, verification.id)
        assert row.state == "pending" and row.message_id
    assert attempts == 2
