"""Real service regressions for delayed decisions, joins and indexed chat checks."""

import asyncio
import time

import pytest
from sqlalchemy import insert, literal, select, text, union_all

from conftest import drain, join, message
from purrai.action_queue import enqueue
from purrai.chat_state import blocked_message
from purrai.errors import ManagementError
from purrai.management import Management
from purrai.models import Action, Group, Member, Rule, now
from purrai.schemas import ArchiveFilter
from purrai.service import Service
from test_chat import configured


@pytest.mark.parametrize("lost_after_acceptance", [False, True])
async def test_new_unmute_fences_delayed_manual_mute_across_restart(env, monkeypatch, lost_after_acceptance):
    settings, db, tg, service = env
    management = Management(db, tg, settings=settings)
    original = tg.call
    lose = True

    async def telegram(method, **kwargs):
        nonlocal lose
        if method == "restrict_chat_member" and lose:
            lose = False
            if lost_after_acceptance:
                await original(method, **kwargs)
            raise ConnectionError("lost restriction receipt")
        result = await original(method, **kwargs)
        # Telegram can return ordinary membership when all default rights have
        # been restored. The historical fake always returned 'restricted'.
        if method == "restrict_chat_member" and all(kwargs["permissions"].values()):
            tg.members[(kwargs["chat_id"], kwargs["user_id"])] = {"status": "member"}
        return result

    monkeypatch.setattr(tg, "call", telegram)
    old = await management.member_action(1, -1001, 42, "mute", "older-manual-mute")
    await service.run_one_action(include_progress=False, include_cleanup=False)
    latest = await management.member_action(1, -1001, 42, "unmute", "newer-manual-unmute")
    await drain(service)
    assert tg.members[(-1001, 42)]["status"] == "member"
    async with db.transaction() as session:
        assert (await session.get(Action, old["id"])).state == "cancelled"
        assert (await session.get(Action, latest["id"])).state == "succeeded"
        # Also guard legacy/recovered work that was not cancelled at creation.
        row = await session.get(Action, old["id"])
        row.state, row.available_at = "pending", 0
    tg.calls.clear()
    await drain(Service(db, tg, settings))
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.get(Action, old["id"])).state == "cancelled"


async def test_failed_manual_receipt_cannot_override_a_newer_decision_via_retry(env):
    settings, db, tg, service = env
    management = Management(db, tg, settings=settings)
    old = await management.member_action(1, -1001, 42, "mute", "obsolete-failed-mute")
    async with db.transaction() as session:
        (await session.get(Action, old["id"])).state = "failed"
    await management.member_action(1, -1001, 42, "ban", "newer-owner-ban")
    with pytest.raises(ManagementError, match="新的成员处理决定") as rejected:
        await management.retry(1, old["id"])
    assert rejected.value.status == 409
    await drain(service)
    assert tg.members[(-1001, 42)]["status"] == "kicked"


async def test_delayed_action_reserves_only_its_member_stream(env):
    _, db, tg, service = env
    async with db.transaction() as session:
        first = await enqueue(session, "delete", "delayed-first", -1001, 42, {"message_id": 701})
        first.available_at, first.created_at = now() + 60, 100
        second = await enqueue(session, "delete", "delayed-second", -1001, 42, {"message_id": 702})
        third = await enqueue(session, "delete", "other-member", -1001, 43, {"message_id": 703})
        # Windows can assign the same wall-clock timestamp to rapid inserts.
        # Random UUID sorting must never let the second action jump the first.
        second.created_at = third.created_at = 100
    assert await service.run_one_action(include_progress=False, include_cleanup=False)
    assert not await service.run_one_action(include_progress=False, include_cleanup=False)
    assert [data["message_id"] for method, data in tg.calls if method == "delete_message"] == [703]
    async with db.transaction() as session:
        (await session.get(Action, first.id)).available_at = 0
    await drain(service)
    assert [data["message_id"] for method, data in tg.calls if method == "delete_message"] == [703, 701, 702]


async def test_new_decision_during_an_inflight_manual_mutation_wins(env, monkeypatch):
    settings, db, tg, service = env
    management = Management(db, tg, settings=settings)
    old = await management.member_action(1, -1001, 42, "mute", "inflight-old-mute")
    started, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def telegram(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "restrict_chat_member" and not kwargs["permissions"]["can_send_messages"]:
            started.set()
            await finish.wait()
        return result

    monkeypatch.setattr(tg, "call", telegram)
    running = asyncio.create_task(service.run_one_action(include_progress=False, include_cleanup=False))
    try:
        await asyncio.wait_for(started.wait(), 3)
        latest = await management.member_action(1, -1001, 42, "unmute", "inflight-new-unmute")
    finally:
        finish.set()
        await running
    await drain(service)
    assert tg.members[(-1001, 42)]["can_send_messages"]
    async with db.sessions() as session:
        assert (await session.get(Action, old["id"])).state == "cancelled"
        assert (await session.get(Action, latest["id"])).state == "succeeded"


@pytest.mark.parametrize("rejoin", [False, True])
async def test_profile_penalty_always_binds_the_observed_member_generation(env, rejoin):
    _, db, tg, service = env
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="SPAM", scope="name"))
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_enabled": False}
    await service.message(message(11, "普通发言", **{"from": {"id": 42, "first_name": "SPAM"}}))
    async with db.sessions() as session:
        penalty = await session.scalar(select(Action).where(Action.kind == "profile_mute"))
        member = await session.get(Member, (-1001, 42))
        assert penalty.payload["generation"] == member.generation
    if rejoin:
        await service.member_event(join(old="member", new="left"), event_id=2)
        await service.member_event(join(old="left", new="member"), event_id=3)
    await drain(service)
    async with db.sessions() as session:
        assert (await session.get(Action, penalty.id)).state == ("cancelled" if rejoin else "succeeded")
    assert any(method == "restrict_chat_member" for method, _ in tg.calls) is not rejoin


async def test_legacy_unbound_profile_penalty_never_mutates_a_current_join(env):
    _, db, tg, service = env
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, generation="new-join"))
        old = await enqueue(session, "profile_mute", "legacy-unbound", -1001, 42, {"generation": ""})
    await drain(service)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.get(Action, old.id)).state == "cancelled"


@pytest.mark.parametrize("operation", ["ban", "link", "export"])
async def test_queued_operation_rechecks_the_issuing_administrators_current_role(env, operation):
    settings, db, tg, service = env
    tg.members[(-1001, 2)] = {"status": "administrator"}
    management = Management(db, tg, settings=settings)
    if operation == "ban":
        receipt = await management.member_action(2, -1001, 42, "ban", "actor-role-ban")
    elif operation == "link":
        async with db.transaction() as session:
            group = await session.get(Group, -1001)
            group.settings = {**group.settings, "join_requests": True}
        receipt = await management.join_link(2, -1001, "actor-role-link")
    else:
        await service.message(message(30, "可导出消息"))
        receipt = await management.archive_export(2, -1001, ArchiveFilter(), "actor-role-export")
    tg.members[(-1001, 2)] = {"status": "member"}
    tg.calls.clear()
    await drain(service)
    async with db.sessions() as session:
        assert (await session.get(Action, receipt["id"])).state == "cancelled"
    assert not any(method in ("ban_chat_member", "create_chat_invite_link", "send_document") for method, _ in tg.calls)


async def test_demotion_during_a_target_lookup_prevents_the_next_effect(env, monkeypatch):
    settings, db, tg, service = env
    tg.members[(-1001, 2)] = {"status": "administrator"}
    management = Management(db, tg, settings=settings)
    receipt = await management.member_action(2, -1001, 42, "ban", "actor-demoted-mid-lookup")
    original = tg.call

    async def demote(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_chat_member" and kwargs["user_id"] == 42:
            tg.members[(-1001, 2)] = {"status": "member"}
        return result

    monkeypatch.setattr(tg, "call", demote)
    await drain(service)
    async with db.sessions() as session:
        assert (await session.get(Action, receipt["id"])).state == "cancelled"
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)


async def test_transient_authorization_failure_retries_without_losing_delivery_intent(env):
    settings, db, tg, service = env
    tg.members[(-1001, 2)] = {"status": "administrator"}
    management = Management(db, tg, settings=settings)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "join_requests": True}
    receipt = await management.join_link(2, -1001, "actor-temporary-failure")
    tg.fail["get_chat_member"] = 1
    await service.run_one_action(include_progress=False, include_cleanup=False)
    async with db.transaction() as session:
        task = await session.get(Action, receipt["id"])
        assert task.state == "pending" and "delivery" not in task.payload
        task.available_at = 0
    await drain(Service(db, tg, settings))
    async with db.sessions() as session:
        task = await session.get(Action, receipt["id"])
        assert task.state == "succeeded" and task.payload["delivery"]["state"] == "confirmed"


@pytest.mark.parametrize("unavailable", [False, True], ids=["demoted", "temporarily-offline"])
async def test_archive_delivery_rechecks_roles_after_export_without_a_false_uncertain_receipt(env, monkeypatch, unavailable):
    from purrai import archive

    settings, db, tg, service = env
    tg.members[(-1001, 2)] = {"status": "administrator"}
    management = Management(db, tg, settings=settings)
    await service.message(message(30, "仅限本群管理员的归档"))
    receipt = await management.archive_export(2, -1001, ArchiveFilter(), "export-role-after-preparation")
    original, changed = archive.export_document, False

    async def prepare(*args):
        nonlocal changed
        result = await original(*args)
        if not changed:
            changed = True
            if unavailable:
                tg.fail["get_chat_member"] = 1
            else:
                tg.members[(-1001, 2)] = {"status": "member"}
        return result

    monkeypatch.setattr(archive, "export_document", prepare)
    await service.run_one_action(include_progress=False, include_cleanup=False)
    async with db.transaction() as session:
        task = await session.get(Action, receipt["id"])
        assert task.state == ("pending" if unavailable else "cancelled")
        assert task.payload["delivery"]["state"] == "rejected"
        task.available_at = 0
    assert not any(method == "send_document" for method, _ in tg.calls)
    if unavailable:
        await drain(Service(db, tg, settings))
        async with db.sessions() as session:
            task = await session.get(Action, receipt["id"])
            assert task.state == "succeeded" and task.payload["delivery"]["state"] == "confirmed"
        assert len([method for method, _ in tg.calls if method == "send_document"]) == 1


async def test_interrupting_read_only_send_preflight_does_not_strand_an_unsent_export(env, monkeypatch):
    from purrai import archive

    settings, db, tg, service = env
    tg.members[(-1001, 2)] = {"status": "administrator"}
    management = Management(db, tg, settings=settings)
    await service.message(message(30, "待发送的归档"))
    receipt = await management.archive_export(2, -1001, ArchiveFilter(), "interrupted-role-preflight")
    original_prepare, original_call = archive.export_document, tg.call
    interrupt = False

    async def prepare(*args):
        nonlocal interrupt
        result = await original_prepare(*args)
        interrupt = True
        return result

    async def telegram(method, **kwargs):
        if interrupt and method == "get_chat_member" and kwargs["user_id"] == 2:
            raise asyncio.CancelledError()
        return await original_call(method, **kwargs)

    monkeypatch.setattr(archive, "export_document", prepare)
    monkeypatch.setattr(tg, "call", telegram)
    with pytest.raises(asyncio.CancelledError):
        await service.run_one_action(include_progress=False, include_cleanup=False)
    async with db.sessions() as session:
        task = await session.get(Action, receipt["id"])
        assert task.state == "pending" and "delivery" not in task.payload
    monkeypatch.setattr(archive, "export_document", original_prepare)
    monkeypatch.setattr(tg, "call", original_call)
    await drain(Service(db, tg, settings))
    async with db.sessions() as session:
        task = await session.get(Action, receipt["id"])
        assert task.state == "succeeded" and task.payload["delivery"]["state"] == "confirmed"
    assert len([method for method, _ in tg.calls if method == "send_document"]) == 1


async def test_chat_checks_use_exact_message_index_with_50000_historical_actions(env, record_property):
    chat, _ = await configured(env)
    _, db, _, _ = env
    async with db.transaction() as session:
        await session.execute(insert(Action), [dict(
            id=f"history-{i}", dedupe_key=f"history-{i}", group_id=-1001, user_id=42,
            kind="delete", state="succeeded", payload={"message_id": i},
        ) for i in range(1000, 51000)])
    candidates = union_all(*(select(literal(i).label("message_id"), literal(42).label("sender_id"))
                             for i in range(1, 51))).subquery()
    query = select(candidates.c.message_id).where(
        blocked_message(-1001, candidates.c.message_id, candidates.c.sender_id)
    )
    async with db.sessions() as session:
        sql = str(query.compile(db.engine.sync_engine, compile_kwargs={"literal_binds": True}))
        plan = (await session.execute(text("EXPLAIN QUERY PLAN " + sql))).all()
        assert any("ix_actions_message" in row[-1] and "<expr>=" in row[-1] for row in plan)
        started = time.perf_counter()
        assert (await session.execute(query)).all() == []
        elapsed = time.perf_counter() - started
        record_property("context_lookup_50_messages_50000_actions_seconds", elapsed)
        assert elapsed < 0.5
    # The actual observation, context preparation, model and delivery pipeline
    # must also work with this history, not only an isolated compiled query.
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    assert await asyncio.wait_for(chat.run_one(), 3)
