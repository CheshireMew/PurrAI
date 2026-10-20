import asyncio

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import EditMessageCaption
from sqlalchemy import select

from conftest import drain, join, message
from purrai.models import Action, Group, Inbox, Meta, Rule, Verification, Violation, now, uid
from purrai.runtime import Runtime
from purrai.service import Service, enqueue
from test_verification import fixed_challenge, latest


async def press(service, verification, key, identifier=None):
    return await service.callback(
        {
            "id": identifier or uid(),
            "from": {"id": verification.user_id},
            "data": f"v:{verification.id}:{verification.nonce}:{key}",
        }
    )


async def due(database):
    async with database.transaction() as session:
        for action in await session.scalars(
            select(Action).where(Action.kind == "progress", Action.state == "pending")
        ):
            action.available_at = 0


def edits(telegram):
    return [args for method, args in telegram.calls if method == "edit_message_caption"]


async def test_six_digits_coalesce_and_overflow_does_not_queue_more(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    for key in "123456789":
        await press(service, verification, key)
    assert (await latest(database)).input_digits == "123456"
    async with database.sessions() as session:
        assert len((await session.scalars(select(Action).where(Action.kind == "progress"))).all()) == 1
    await due(database)
    await service.progress.run_one()
    assert len(edits(telegram)) == 1
    assert "●●●●●●" in edits(telegram)[0]["caption"]


async def test_slow_edit_does_not_block_input_submit_or_release(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await press(service, verification, "1")
    await due(database)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = telegram.call

    async def slow(method, **kwargs):
        if method == "edit_message_caption":
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", slow)
    task = asyncio.create_task(service.progress.run_one())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for key in "23456":
            await asyncio.wait_for(press(service, verification, key), 1)
        await asyncio.wait_for(press(service, verification, "ok"), 1)
        await asyncio.wait_for(service.run_one_action(include_progress=False), 1)
        assert (await latest(database)).state == "passed"
    finally:
        finish.set()
        await task
    async with database.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "progress"))).state == "cancelled"


async def test_input_during_edit_renders_latest_after_restart(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, service = env
    await press(service, verification, "1")
    await due(database)
    original = telegram.call

    async def advance(method, **kwargs):
        if method == "edit_message_caption":
            await press(service, verification, "2")
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", advance)
    await service.progress.run_one()
    monkeypatch.setattr(telegram, "call", original)
    async with database.sessions() as session:
        row = await session.scalar(select(Action).where(Action.kind == "progress"))
        assert row.state == "pending" and row.available_at > now()
    restarted = Service(database, telegram, settings)
    await due(database)
    await restarted.progress.run_one()
    assert "●●○○○○" in edits(telegram)[-1]["caption"]


async def test_same_visible_count_skips_redundant_edit(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await press(service, verification, "1")
    await due(database)
    await service.progress.run_one()
    await press(service, verification, "back")
    await press(service, verification, "2")
    await due(database)
    await service.progress.run_one()
    assert (await latest(database)).input_digits == "2"
    assert len(edits(telegram)) == 1


async def test_retry_after_is_shared_by_chat_and_not_bypassed_by_new_keys(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, service = env
    original = telegram.call

    async def limited(method, **kwargs):
        if method == "edit_message_caption":
            raise TelegramRetryAfter(
                method=EditMessageCaption(chat_id=-1001, message_id=101), message="limited", retry_after=12
            )
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", limited)
    await press(service, verification, "1")
    await due(database)
    await service.progress.run_one()
    async with database.sessions() as session:
        row = await session.scalar(select(Action).where(Action.kind == "progress"))
        retry_at = row.available_at
    await press(service, verification, "2")
    async with database.sessions() as session:
        row = await session.scalar(select(Action).where(Action.kind == "progress"))
        assert row.available_at == retry_at and "限流" in row.error
    # A second member shares the chat cooldown, even after the service restarts.
    await service.member_event(join(user_id=43))
    await drain(service)
    second = await latest(database)
    assert second.user_id == 43
    await press(service, second, "3")
    await due(database)
    monkeypatch.setattr(telegram, "call", original)
    restarted = Service(database, telegram, settings)
    await restarted.progress.run_one()
    await restarted.progress.run_one()
    assert not edits(telegram)
    async with database.sessions() as session:
        pending = (
            await session.scalars(select(Action).where(Action.kind == "progress", Action.state == "pending"))
        ).all()
        assert len(pending) == 2 and all(a.available_at >= retry_at for a in pending)


async def test_legacy_queued_edits_are_compacted(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    async with database.transaction() as session:
        row = await session.get(Verification, verification.id)
        row.input_digits = "1234"
        for i in range(6):
            await enqueue(session, "progress", f"legacy:{i}", -1001, 42, {"verification_id": row.id}, 2)
    await service.progress.run_one()
    await drain(service)
    assert len(edits(telegram)) == 1
    assert "●●●●○○" in edits(telegram)[0]["caption"]


async def test_duplicate_key_concurrently_commits_once(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, _, service = env
    await asyncio.gather(*(press(service, verification, "1", identifier="same-key") for _ in range(6)))
    assert (await latest(database)).input_digits == "1"
    async with database.sessions() as session:
        assert (await session.get(Meta, "callback:same-key")).value == "已输入 1 / 6 位"


async def test_slow_callback_ack_does_not_block_durable_inbox(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = telegram.call

    async def slow(method, **kwargs):
        if method == "answer_callback_query":
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", slow)
    try:
        for i, key in enumerate("123456", 100):
            await runtime.persist(
                [
                    {
                        "update_id": i,
                        "callback_query": {
                            "id": str(i),
                            "chat_instance": "test",
                            "from": {"id": 42, "is_bot": False, "first_name": "测试"},
                            "data": f"v:{verification.id}:{verification.nonce}:{key}",
                        },
                    }
                ]
            )
        for _ in range(6):
            assert await asyncio.wait_for(runtime.process_one(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        assert (await latest(database)).input_digits == "123456"
        async with database.sessions() as session:
            assert all(item.state == "done" for item in await session.scalars(select(Inbox)))
    finally:
        finish.set()
        await runtime.stop()


async def test_plain_group_messages_do_not_query_telegram_and_admin_stays_exempt(env):
    _, database, telegram, service = env
    await service.message(message(100, "正常聊天"))
    assert not any(method == "get_chat_member" for method, _ in telegram.calls)
    telegram.members[(-1001, 42)] = {"status": "administrator"}
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="广告"))
    await service.message(message(101, "广告"))
    assert any(method == "get_chat_member" for method, _ in telegram.calls)
    async with database.sessions() as session:
        assert not (await session.scalars(select(Violation))).all()


async def test_failed_refresh_retries_latest_input_without_verification_error(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await press(service, verification, "1")
    await due(database)
    telegram.fail["edit_message_caption"] = 1
    await service.progress.run_one()
    await press(service, verification, "2")
    assert (await latest(database)).error == ""
    await due(database)
    await service.progress.run_one()
    assert "●●○○○○" in edits(telegram)[-1]["caption"]


async def test_slow_moderation_lookup_does_not_hold_input_and_reloads_settings(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="广告"))
    entered, finish = asyncio.Event(), asyncio.Event()
    original = telegram.call

    async def slow(method, **kwargs):
        if method == "get_chat_member" and kwargs["user_id"] == 43:
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", slow)
    task = asyncio.create_task(service.message(message(900, "广告", user_id=43)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(press(service, verification, "1"), 1)
        assert (await latest(database)).input_digits == "1"
        async with database.transaction() as session:
            (await session.get(Group, -1001)).enabled = False
    finally:
        finish.set()
        await task
    async with database.sessions() as session:
        assert not (await session.scalars(select(Violation))).all()


def callback_update(verification, identifier, key):
    return {
        "update_id": identifier,
        "callback_query": {
            "id": str(identifier),
            "chat_instance": "test",
            "from": {"id": verification.user_id, "is_bot": False, "first_name": "测试"},
            "data": f"v:{verification.id}:{verification.nonce}:{key}",
        },
    }


async def test_callback_lane_passes_slow_message_without_mixing_update_ids(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="广告"))
    incoming = message(600, "广告", user_id=43)
    incoming["from"].update(is_bot=False, first_name="其他成员")
    await runtime.persist(
        [{"update_id": 1000, "message": incoming}, callback_update(verification, 1001, "1")]
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    original = telegram.call
    seen = []
    original_message = service.message

    async def record_message(payload, **kwargs):
        await original_message(payload, **kwargs)
        seen.append((kwargs["update_id"], runtime.current_update_id.get()))

    async def slow(method, **kwargs):
        if method == "get_chat_member" and kwargs["user_id"] == 43:
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", slow)
    monkeypatch.setattr(service, "message", record_message)
    task = asyncio.create_task(runtime.process_one("messages"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await asyncio.wait_for(runtime.process_one("verification"), 1)
        assert (await latest(database)).input_digits == "1"
    finally:
        finish.set()
        await task
        await runtime.stop()
    assert seen == [(1000, 1000)]
    assert runtime.current_update_id.get() == 0


async def test_callback_lane_waits_for_earlier_membership_change(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    leave = join(old="member", new="left")
    leave["from"].update(is_bot=False, first_name="管理员")
    for field in ("old_chat_member", "new_chat_member"):
        leave[field]["user"]["is_bot"] = False
    await runtime.persist(
        [{"update_id": 2000, "chat_member": leave}, callback_update(verification, 2001, "1")]
    )
    try:
        assert not await runtime.process_one("verification")
        assert await runtime.process_one("messages")
        assert await runtime.process_one("verification")
        assert (await latest(database)).state == "left"
        assert (await latest(database)).input_digits == ""
    finally:
        await runtime.stop()


async def test_callback_lane_never_skips_a_retrying_digit(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await runtime.persist(
        [callback_update(verification, 3000, "1"), callback_update(verification, 3001, "2")]
    )
    async with database.transaction() as session:
        (await session.get(Inbox, 3000)).available_at = now() + 10
    try:
        assert not await runtime.process_one("verification")
        assert (await latest(database)).input_digits == ""
        async with database.transaction() as session:
            (await session.get(Inbox, 3000)).available_at = 0
        assert await runtime.process_one("verification")
        assert await runtime.process_one("verification")
        assert (await latest(database)).input_digits == "12"
    finally:
        await runtime.stop()
