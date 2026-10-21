"""Failures observed during the frontend/backend architecture review."""

import asyncio
import hashlib

import pytest
from sqlalchemy import select

from conftest import drain, join, message
from purrai.management import Management, ManagementError
from purrai.models import Action, Group, Inbox, Member, Session, Verification, default_settings, now
from purrai.runtime import Runtime
from purrai.schemas import ActionInput
from purrai.service import enqueue
from test_verification import fixed_challenge, latest


async def test_verification_kick_finishes_unban_when_own_departure_arrives_during_ban(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_failure_action": "kick"}
        (await session.get(Verification, verification.id)).deadline = now() - 1
    original = tg.call

    async def notify(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "ban_chat_member":
            event = join(actor=tg.user_id, old="restricted", new="kicked")
            event["old_chat_member"]["is_member"] = True
            event["new_chat_member"]["until_date"] = kwargs["until_date"]
            await service.member_event(event, event_id=100)
        return result

    monkeypatch.setattr(tg, "call", notify)
    await service.scan_timeouts()
    await drain(service)
    assert tg.members[(-1001, 42)]["status"] == "left"
    assert sum(method == "unban_chat_member" for method, _ in tg.calls) == 1
    current = await latest(db)
    assert (current.state, current.failure_action) == ("failed", "kick")
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "verification_failure"))).state == "succeeded"


@pytest.mark.parametrize("generation", ["old-join", "new-join"])
async def test_historical_verification_cannot_unban_a_replacement_restriction(env, generation):
    settings, db, tg, service = env
    until = int(now() + 600)
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, generation=generation, present=False,
                           owned_restriction={"kind": "ban", "source_action": "new-ban", "until_date": until}))
        session.add(Verification(id="historical", group_id=-1001, user_id=42, generation="old-join",
                                 state="failed", failure_action="legacy_ban", deadline=now() - 600))
        await enqueue(session, "manual_ban", "new-ban", -1001, 42, actor_id=1)
    tg.members[(-1001, 42)] = {"status": "kicked", "until_date": until}
    with pytest.raises(ManagementError) as error:
        await Management(db, tg, service, settings).verification_action(
            1, "historical", ActionInput(action="unban", reason="核实旧记录", request_id="old-ban-correction")
        )
    assert error.value.status == 409
    assert not tg.calls
    assert tg.members[(-1001, 42)]["status"] == "kicked"


async def test_legacy_queued_unban_is_cancelled_after_rejoin(env):
    _, db, tg, service = env
    until = int(now() + 600)
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, generation="new-join", present=False,
                           owned_restriction={"kind": "ban", "source_action": "new-ban", "until_date": until}))
        session.add(Verification(id="historical", group_id=-1001, user_id=42, generation="old-join",
                                 state="failed", failure_action="legacy_ban", deadline=now() - 600))
        action = await enqueue(session, "unban", "legacy-unban", -1001, 42,
                               {"source_verification_id": "historical", "request_cooldown": False}, actor_id=1)
        identifier = action.id
    tg.members[(-1001, 42)] = {"status": "kicked", "until_date": until}
    await drain(service)
    assert not tg.calls
    async with db.sessions() as session:
        assert (await session.get(Action, identifier)).state == "cancelled"
        assert (await session.get(Member, (-1001, 42))).owned_restriction["source_action"] == "new-ban"


async def test_pending_member_unban_cannot_clear_a_later_ban_in_the_same_join(env):
    settings, db, tg, service = env
    until = int(now() + 600)
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, generation="same-join", present=False,
                           owned_restriction={"kind": "ban", "source_action": "first-ban", "until_date": until}))
    task = await Management(db, tg, service, settings).member_action(1, -1001, 42, "unban", "pending-unban")
    async with db.transaction() as session:
        member = await session.get(Member, (-1001, 42))
        member.owned_restriction = {"kind": "ban", "source_action": "second-ban", "until_date": until}
    tg.members[(-1001, 42)] = {"status": "kicked", "until_date": until}
    await drain(service)
    assert not tg.calls
    async with db.sessions() as session:
        assert (await session.get(Action, task["id"])).state == "cancelled"
        assert (await session.get(Member, (-1001, 42))).owned_restriction["source_action"] == "second-ban"


@pytest.mark.parametrize("url", ["http://[", "https://example..com", "http://\uff0f", "https://t.me\uff0fjoinchat/abc"])
async def test_invalid_url_cannot_discard_other_moderation_results(env, url):
    _, db, tg, service = env
    from purrai.models import Rule, Violation

    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="SPAM"))
    await service.message(message(20, f"SPAM {url}"))
    await drain(service)
    assert [kwargs["message_id"] for method, kwargs in tg.calls if method == "delete_message"] == [20]
    async with db.sessions() as session:
        assert "关键词：SPAM" in (await session.scalar(select(Violation))).reasons


async def test_web_authorizes_target_once_and_lists_groups_once(env, client):
    _, db, tg, _ = env
    async with db.transaction() as session:
        for identifier in (-1002, -1003):
            session.add(Group(id=identifier, approved=True, settings=default_settings()))
        session.add(Session(id=hashlib.sha256(b"scope-audit").hexdigest(), user_id=2,
                            csrf="audit", expires_at=now() + 60))
    tg.members[(-1001, 2)] = {"status": "administrator"}
    client.cookies.set("purrai_session", "scope-audit")
    for path, count in (("/api/me", 3), ("/api/groups", 3), ("/api/dashboard", 3),
                        ("/api/groups/-1001/rules", 1)):
        tg.calls.clear()
        response = await client.get(path)
        assert response.status_code == 200
        assert len([method for method, _ in tg.calls if method == "get_chat_member"]) == count
    tg.members[(-1001, 2)] = {"status": "member"}
    assert (await client.get("/api/groups/-1001/rules")).status_code == 403
    assert (await client.get("/api/me")).status_code == 403


async def test_slow_notification_does_not_block_another_members_verification(env, monkeypatch):
    _, db, tg, service = env
    async with db.transaction() as session:
        await enqueue(session, "warn", "slow-notification", -1001, 42, {"text": "通知"}, priority=20)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "send_message":
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    task = asyncio.create_task(service.run_one_action(include_progress=False, include_cleanup=False))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await service.member_event(join(43), event_id=1)
        assert await asyncio.wait_for(service.run_one_action(
            include_progress=False, include_cleanup=False, lane="member"
        ), 2)
        assert not tg.members[(-1001, 43)]["can_send_messages"]
    finally:
        finish.set()
        await task


async def test_action_claims_serialize_same_member_without_blocking_other_members(env, monkeypatch):
    _, db, tg, service = env
    async with db.transaction() as session:
        for identifier, user in ((701, 42), (702, 42), (703, 43)):
            await enqueue(session, "delete", f"claim:{identifier}", -1001, user,
                          {"message_id": identifier}, actor_id=1)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "delete_message" and kwargs["message_id"] == 701:
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    first = asyncio.create_task(service.run_one_action(include_progress=False, include_cleanup=False))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await asyncio.wait_for(service.run_one_action(include_progress=False, include_cleanup=False), 2)
        assert not await service.run_one_action(include_progress=False, include_cleanup=False)
        assert [kwargs["message_id"] for method, kwargs in tg.calls if method == "delete_message"] == [703]
    finally:
        finish.set()
        await first
    await drain(service)
    assert [kwargs["message_id"] for method, kwargs in tg.calls if method == "delete_message"] == [703, 701, 702]
    assert not service.action_claims.identifiers


async def test_inbox_keeps_group_order_and_processes_another_group_while_one_is_slow(env, monkeypatch):
    _, db, tg, service = env
    async with db.transaction() as session:
        session.add(Group(id=-1002, approved=True, enabled=True, settings=default_settings()))
    runtime = Runtime(db, tg, service)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = service.message
    observed = []

    async def slow(incoming, *args, **kwargs):
        if incoming["message_id"] == 701:
            entered.set()
            await finish.wait()
        observed.append(incoming["message_id"])
        await original(incoming, *args, **kwargs)

    monkeypatch.setattr(service, "message", slow)
    updates = []
    for update_id, identifier, group_id in ((1, 701, -1001), (2, 702, -1001), (3, 703, -1002)):
        incoming = message(identifier, "hello", chat={"id": group_id, "type": "supergroup"})
        incoming["from"].update(is_bot=False, first_name="成员")
        updates.append({"update_id": update_id, "message": incoming})
    await runtime.persist(updates)
    first = asyncio.create_task(runtime.process_one("messages"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await asyncio.wait_for(runtime.process_one("messages"), 2)
        assert not await runtime.process_one("messages")
        async with db.sessions() as session:
            assert (await session.get(Inbox, 3)).state == "done"
            assert (await session.get(Inbox, 2)).state == "pending"
    finally:
        finish.set()
        await first
    try:
        assert await runtime.process_one("messages")
        assert observed == [703, 701, 702]
        assert not runtime.inbox_claims.identifiers
    finally:
        await runtime.stop()


async def test_delayed_inbox_item_holds_only_its_own_group(env):
    _, db, tg, service = env
    async with db.transaction() as session:
        session.add(Group(id=-1002, approved=True, enabled=True, settings=default_settings()))
    runtime = Runtime(db, tg, service)
    updates = []
    for identifier, group_id in ((1, -1001), (2, -1001), (3, -1002)):
        incoming = message(identifier, "hello", chat={"id": group_id, "type": "supergroup"})
        incoming["from"].update(is_bot=False, first_name="成员")
        updates.append({"update_id": identifier, "message": incoming})
    await runtime.persist(updates)
    async with db.transaction() as session:
        (await session.get(Inbox, 1)).available_at = now() + 60
    try:
        assert await runtime.process_one("messages")
        assert not await runtime.process_one("messages")
        async with db.transaction() as session:
            assert (await session.get(Inbox, 3)).state == "done"
            assert (await session.get(Inbox, 2)).state == "pending"
            (await session.get(Inbox, 1)).available_at = 0
        assert await runtime.process_one("messages")
        assert await runtime.process_one("messages")
    finally:
        await runtime.stop()


async def test_running_workers_moderate_group_while_private_panel_is_waiting_on_telegram(env, monkeypatch):
    _, db, tg, service = env
    from purrai.models import Rule
    from test_bot_admin import private

    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="SPAM"))
    runtime = Runtime(db, tg, service)
    entered = asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "send_message" and kwargs["chat_id"] == 1:
            entered.set()
            await asyncio.Future()
        return await original(method, **kwargs)

    async def idle_poll():
        await asyncio.Future()

    monkeypatch.setattr(tg, "call", slow)
    monkeypatch.setattr(runtime, "poll_loop", idle_poll)
    control = private("/admin", 1)
    control["from"].update(is_bot=False, first_name="管理员")
    await runtime.persist([{"update_id": 1, "message": control}])
    runtime.start()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        incoming = message(703, "SPAM")
        incoming["from"].update(is_bot=False, first_name="成员")
        await runtime.persist([{"update_id": 2, "message": incoming}])

        async def processed():
            while True:
                async with db.sessions() as session:
                    item = await session.get(Inbox, 2)
                    action = await session.scalar(select(Action).where(Action.kind == "delete"))
                    if item.state == "done" and action and action.state == "succeeded":
                        return
                await asyncio.sleep(0.02)

        await asyncio.wait_for(processed(), 3)
        async with db.sessions() as session:
            assert (await session.get(Inbox, 1)).state == "pending"
    finally:
        await runtime.stop()
    assert not runtime.inbox_claims.identifiers
    assert not service.action_claims.identifiers


async def test_batch_of_125_members_needs_one_progress_request_and_preserves_outcomes(env, client):
    _, db, _, _ = env
    async with db.transaction() as session:
        for user_id in range(100, 225):
            session.add(Verification(group_id=-1001, user_id=user_id, generation="test",
                                     state="pending", deadline=now() + 180))
    response = await client.post("/api/groups/-1001/release-pending", json={
        "action": "release", "reason": "核实后放行", "request_id": "large-batch-request",
    })
    assert response.status_code == 200
    receipt = response.json()
    assert len(receipt["action_ids"]) == 125
    progress = (await client.get(f"/api/actions/{receipt['batch_id']}")).json()
    assert progress["state"] == "pending" and progress["summary"]["pending"] == 125
    async with db.transaction() as session:
        children = (await session.scalars(select(Action).where(Action.batch_id == receipt["batch_id"])) ).all()
        assert len(children) == 125
        for index, child in enumerate(children):
            child.state = "failed" if index == 0 else "cancelled" if index == 1 else "succeeded"
            child.error = "Telegram 权限不足" if index == 0 else ""
    progress = await client.get(f"/api/actions/{receipt['batch_id']}")
    assert progress.status_code == 200
    assert progress.json()["summary"] == {"total": 125, "pending": 0, "succeeded": 123, "failed": 1, "cancelled": 1}
    assert progress.json()["state"] == "failed" and "Telegram 权限不足" in progress.json()["error"]
