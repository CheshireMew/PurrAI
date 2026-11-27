"""Failure boundaries and cross-layer contracts from the November architecture review."""

import asyncio
import os
import threading

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session as SyncSession

from conftest import message
from purrai.models import Action, AdvertisementReview, Group, Inbox, Meta, PointsAccount, PointsDaily, PointsEntry, now
from purrai.points_bot import PointsBot
from purrai.points_policy import default_policy
from purrai.points_schemas import MAX_POINTS
from purrai.runtime import Runtime
from purrai.work_claims import WorkClaims


async def test_migration_conflict_is_a_recoverable_input_not_a_polling_barrier(env):
    settings, db, tg, service = env
    runtime = Runtime(db, tg, service)
    async with db.transaction() as session:
        session.add(Group(id=-1002, approved=True))
    migration = {"update_id": 100, "message": {**message(1, ""), "migrate_to_chat_id": -1002}}
    ordinary = {"update_id": 101, "message": message(2, "ordinary", chat={"id": -1002, "type": "supergroup"})}
    ordinary["message"]["from"].update(first_name="Audit", is_bot=False)
    await runtime.persist([migration, ordinary])
    async with db.sessions() as session:
        failed = await session.get(Inbox, 100)
        assert failed.state == "failed" and failed.payload == migration
        assert (await session.get(Inbox, 101)).state == "pending"
        assert (await session.get(Meta, "poll_offset")).value == "102"
    await runtime.management.retry(1, 100, event=True, request_id="migration-retry")
    # The conflict stays local on retry, while another group's input can run.
    assert await runtime.process_one()
    assert await runtime.process_one()
    async with db.sessions() as session:
        assert (await session.get(Inbox, 101)).state == "done"
        assert (await session.get(Inbox, 100)).error
    await runtime.stop()


async def test_new_update_number_epoch_preserves_atomic_checkpoint(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    async with db.transaction() as session:
        session.add_all([Meta(key="poll_offset", value="9000"),
                         Meta(key="poll_offset_at", value=str(now() - 8 * 86400))])
    await runtime.persist([{"update_id": 100, "message": message(1, "new epoch")}])
    async with db.sessions() as session:
        assert (await session.get(Meta, "poll_offset")).value == "101"
        assert await session.get(Inbox, 100)
    await runtime.persist([{"update_id": 99, "message": message(2, "delayed original")}])
    async with db.sessions() as session:
        assert (await session.get(Meta, "poll_offset")).value == "101"
    await runtime.stop()


async def test_delayed_chat_reward_does_not_reserve_interactive_notification_scope(env):
    _, db, _, service = env
    from purrai.action_queue import enqueue
    async with db.transaction() as session:
        reward = await enqueue(session, "points_message", "waiting-reward", -1001, 42,
                               {"operation": "chat"})
        reward.available_at = now() + 47 * 3600
        command = await enqueue(session, "points_message", "ready-signin", -1001, 42,
                                {"operation": "signin"})
        expected = command.id
    claimed = await service.actions.claim_action(include_progress=False, cleanup_only=False, lane="notification")
    assert claimed[0] == expected
    service.action_claims.release(claimed[0], claimed[2])


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_claim_handoff_releases_only_its_own_reservations(cancelled):
    claims = WorkClaims()
    assert claims.claim("existing", "existing-scope")
    error = asyncio.CancelledError if cancelled else OSError
    with pytest.raises(error):
        async with claims.selecting():
            assert claims.claim("new", "new-scope")
            raise error()
    assert claims.identifiers == {"existing"} and claims.scopes == {"existing-scope"}


async def test_failed_claim_does_not_leak_a_scope_reused_after_another_worker_finishes():
    claims = WorkClaims()
    assert claims.claim("old", "same-scope")
    with pytest.raises(OSError):
        async with claims.selecting():
            claims.release("old", "same-scope")
            assert claims.claim("new", "same-scope")
            raise OSError()
    assert not claims.identifiers and not claims.scopes and not claims.owners


@pytest.mark.parametrize("kind", ["chat", "advertisement"])
async def test_claim_commit_failure_cannot_leak_busy_scope(env, monkeypatch, kind):
    _, db, tg, service = env
    from test_chat import configured, due
    if kind == "chat":
        worker, _ = await configured(env)
        await worker.observe(message(5, "@purrai_bot 周末去哪里散步", **{"from": {"id": 42, "first_name": "Audit"}}))
        await due(worker)
    else:
        worker = service.advertisement_review
        await worker.observe(message(5, "独立审核中的普通文字"))
        async with db.transaction() as session:
            row = await session.scalar(select(AdvertisementReview))
            row.local_after = now() + 60
    def interrupted_commit(session):
        if session.bind is db.engine.sync_engine:
            raise OSError("commit interrupted")
    event.listen(SyncSession, "before_commit", interrupted_commit)
    try:
        with pytest.raises(OSError):
            await worker.claim()
    finally:
        event.remove(SyncSession, "before_commit", interrupted_commit)
    assert not worker.busy
    assert await worker.claim()
    worker.busy.clear()
    if kind == "chat":
        await worker.close()


@pytest.mark.parametrize("operation", ["points_create", "points_settings", "lottery_save"])
async def test_new_management_writes_recheck_group_under_writer_lock(client, env, monkeypatch, operation):
    _, db, _, _ = env
    original = client._transport.app.state.management.access.authorize

    async def revoke_after_authorizing(*args, **kwargs):
        group = await original(*args, **kwargs)
        async with db.transaction() as session:
            (await session.get(Group, -1001)).approved = False
        return group

    monkeypatch.setattr(client._transport.app.state.management.access, "authorize", revoke_after_authorizing)
    if operation == "points_create":
        response = await client.post("/api/groups/-1001/points", json={"user_id": 42, "display_name": "Audit"},
                                     headers={"Idempotency-Key": "revoke-points"})
    elif operation == "points_settings":
        response = await client.put("/api/groups/-1001/points/settings",
            json={"config": default_policy().model_dump(), "version": 0}, headers={"Idempotency-Key": "revoke-policy"})
    else:
        response = await client.post("/api/groups/-1001/lotteries", json={"config": {
            "title": "Audit", "prizes": [{"name": "Prize"}]}}, headers={"Idempotency-Key": "revoke-lottery"})
    assert response.status_code == 403, response.text


async def test_failed_signin_credit_does_not_consume_the_daily_opportunity(env):
    _, db, tg, service = env
    from purrai.models import PointsPolicy
    config = default_policy().model_dump()
    config["enabled"] = True
    async with db.transaction() as session:
        session.add(PointsPolicy(group_id=-1001, config=config))
        session.add(PointsAccount(group_id=-1001, source_key="audit", user_id=42,
                                 display_name="Audit", balance=MAX_POINTS))
    points = PointsBot(db, tg)
    phrase = config["commands"]["signin"][0]
    await points.observe(message(1, phrase, **{"from": {"id": 42, "first_name": "Audit"}}))
    await service.run_one_action(include_cleanup=False, lane="notification")
    async with db.transaction() as session:
        assert not await session.scalar(select(PointsDaily.signed))
        assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 0
        account = await session.scalar(select(PointsAccount))
        account.balance -= 100
    await points.observe(message(2, phrase, **{"from": {"id": 42, "first_name": "Audit"}}))
    await service.run_one_action(include_cleanup=False, lane="notification")
    async with db.sessions() as session:
        assert await session.scalar(select(PointsDaily.signed))
        assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 1


async def test_review_claim_recovers_a_committed_handoff_without_taking_a_chat_owned_review(env):
    _, db, _, service = env
    from purrai.advertisement_store import revision
    async with db.transaction() as session:
        for identifier, shared in ((800, False), (801, True)):
            payload = message(identifier, "independent queued review")
            if shared:
                payload["_chat_turn_id"] = 99
            session.add(AdvertisementReview(id=f"orphan-{identifier}", group_id=-1001, message_id=identifier,
                user_id=42, revision=revision(payload), message=payload, event_at=now(),
                state="requesting", local_after=now() + 60))
    worker = service.advertisement_review
    claimed = await worker.claim()
    assert [row.id for row in claimed[0]] == ["orphan-800"]
    async with db.sessions() as session:
        assert (await session.get(AdvertisementReview, "orphan-801")).state == "requesting"
    worker.busy.clear()


async def test_settings_origin_and_backup_plan_reject_stale_writes(client, env):
    _, db, _, _ = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        value = group.settings["chat_enabled"]
        group.setting_overrides = ["chat_enabled"]
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {"chat_enabled": value},
        "baseline": {"chat_enabled": value}, "baseline_overrides": [], "request_id": "origin-conflict"})
    assert response.status_code == 409
    status = (await client.get("/api/system/backups")).json()
    original = {**status["plan"], "version": status["version"], "request_id": "plan-first"}
    first = await client.patch("/api/system/backups/plan", json=original)
    assert first.status_code == 200, first.text
    assert (await client.patch("/api/system/backups/plan", json={**original, "request_id": "plan-stale"})).status_code == 409
    assert (await client.patch("/api/system/backups/plan", json=original)).json() == first.json()
    assert (await client.patch("/api/system/backups/plan", json={**original, "enabled": not original["enabled"]})).status_code == 409


async def test_reset_replay_does_not_repeat_or_replace_another_reset(client, env):
    _, db, _, _ = env
    path = "/api/groups/-1001/chat/reset"
    assert (await client.post(path)).status_code == 422
    assert (await client.post(path, headers={"Idempotency-Key": "reset-first"})).status_code == 200
    assert (await client.post(path, headers={"Idempotency-Key": "reset-second"})).status_code == 200
    async with db.sessions() as session:
        checkpoint = (await session.get(Meta, "chat_reset:-1001")).value
    assert (await client.post(path, headers={"Idempotency-Key": "reset-first"})).status_code == 200
    async with db.sessions() as session:
        assert (await session.get(Meta, "chat_reset:-1001")).value == checkpoint
        assert await session.scalar(select(func.count()).select_from(Action).where(Action.reason == "重新开始群聊天")) == 2


async def test_incomplete_legacy_backup_recovers_same_operation_without_overwriting(env, monkeypatch):
    settings, db, tg, _ = env
    from purrai.management import Management
    manager = Management(db, tg, settings=settings)
    original = db.backup
    async def incomplete(directory, *, filename):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_bytes(b"interrupted legacy snapshot")
        raise OSError("interrupted copy")
    monkeypatch.setattr(db, "backup", incomplete)
    with pytest.raises(OSError):
        await manager.backups.create(1, "partial-backup")
    old = next(settings.backup_dir.glob("*.sqlite3"))
    monkeypatch.setattr(db, "backup", original)
    result = await manager.backups.create(1, "partial-backup")
    assert result["file"] != old.name and old.read_bytes() == b"interrupted legacy snapshot"
    assert await manager.backups.create(1, "partial-backup") == result
    assert len(list(settings.backup_dir.glob("*.sqlite3"))) == 2


async def test_backup_publishes_only_complete_snapshot_and_same_destination_can_retry(env, monkeypatch):
    settings, db, _, _ = env
    name = db.backup_filename()
    original = os.link
    def fail_publish(*args, **kwargs):
        raise OSError("injected publication failure")
    monkeypatch.setattr(os, "link", fail_publish)
    with pytest.raises(OSError):
        await db.backup(settings.backup_dir, filename=name)
    assert not (settings.backup_dir / name).exists()
    assert not list(settings.backup_dir.glob("*.partial"))
    monkeypatch.setattr(os, "link", original)
    assert (await db.backup(settings.backup_dir, filename=name)).is_file()


async def test_policy_cpu_handoff_allows_writes_and_discards_a_changed_snapshot(env):
    _, db, _, _ = env
    from purrai.action_execution import ActionSuperseded
    from purrai.policy_work import PolicyExecution, policy_work
    from purrai.models import Rule
    started, release = threading.Event(), threading.Event()
    def compute():
        started.set()
        assert release.wait(5)
        return "old result"
    async def work():
        async with PolicyExecution(db) as session:
            group = await session.get(Group, -1001)
            assert group
            await policy_work(session, compute)
    task = asyncio.create_task(work())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        async with asyncio.timeout(2):
            async with db.transaction() as session:
                session.add(Rule(group_id=-1001, kind="keyword", value="replaceable-test-input"))
        release.set()
        with pytest.raises(ActionSuperseded):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_ten_thousand_import_uses_batched_sql_and_atomic_entries(env):
    _, db, _, _ = env
    from purrai.points import PointsLedger
    from purrai.points_schemas import PointsImportInput
    body = PointsImportInput(batch_key="large-import", source="architecture regression", rows=[
        {"source_key": str(i), "display_name": f"Member {i}", "points": i // 2, "user_id": i + 1000}
        for i in range(10000)])
    statements = []
    def counted(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.engine.sync_engine, "before_cursor_execute", counted)
    try:
        result = await PointsLedger(db).import_data(-1001, body, 1)
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", counted)
    assert result["count"] == 10000 and len(statements) < 200
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PointsAccount)) == 10000
        assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 10000
        assert await session.scalar(select(func.sum(PointsEntry.delta))) == sum(i // 2 for i in range(10000))
    page = await PointsLedger(db).page(-1001)
    expected = sorted(range(10000), key=lambda i: -(i // 2))[:20]
    assert [row["display_name"] for row in page["items"]] == [f"Member {i}" for i in expected]


@pytest.mark.parametrize("remove", [False, True])
async def test_policy_handoff_bulk_refresh_still_detects_a_changed_or_deleted_source(env, remove):
    _, db, _, _ = env
    from purrai.action_execution import ActionSuperseded
    from purrai.policy_work import PolicyExecution
    async with db.transaction() as session:
        session.add_all(Meta(key=f"bulk-source:{i}", value="original") for i in range(100))
    statements = []
    def counted(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    async def operation():
        async with db.transaction() as session:
            row = await session.get(Meta, "bulk-source:99")
            if remove:
                await session.delete(row)
            else:
                row.value = "changed"
    event.listen(db.engine.sync_engine, "before_cursor_execute", counted)
    try:
        with pytest.raises(ActionSuperseded):
            async with PolicyExecution(db) as session:
                rows = list(await session.scalars(select(Meta).where(Meta.key.startswith("bulk-source:"))))
                assert len(rows) == 100
                await session.info["action_execution"].external(operation)
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", counted)
    assert len(statements) <= 6


@pytest.mark.parametrize("change", ["unchanged", "edited", "blocked"])
async def test_policy_handoff_handles_many_sources_and_rechecks_the_last_source(env, monkeypatch, change):
    _, db, _, _ = env
    from purrai.action_execution import ActionSuperseded
    from purrai.models import ArchivedMessage, Violation
    from purrai.policy_work import PolicyExecution, policy_work

    async with db.transaction() as session:
        await session.execute(ArchivedMessage.__table__.insert(), [
            {"id": f"policy-source:{identifier}", "group_id": -1001, "message_id": identifier,
             "sender_id": 42, "text": "original source", "sent_at": now()}
            for identifier in range(1, 502)
        ])

    async with PolicyExecution(db) as session:
        group = await session.get(Group, -1001)
        assert group
        session.info["policy_messages"] = {(-1001, identifier): 42 for identifier in range(1, 502)}
        execution = session.info["action_execution"]
        original_external = execution.external

        async def concurrent_change(operation):
            async def compute_and_change():
                result = await operation()
                async with db.transaction() as other:
                    if change == "edited":
                        row = await other.get(ArchivedMessage, "policy-source:501")
                        row.text = "edited source"
                    elif change == "blocked":
                        other.add(Violation(group_id=-1001, message_id=501, user_id=42))
                return result
            return await original_external(compute_and_change)

        monkeypatch.setattr(execution, "external", concurrent_change)
        if change == "unchanged":
            assert await policy_work(session, lambda: "current result") == "current result"
        else:
            with pytest.raises(ActionSuperseded):
                await policy_work(session, lambda: "outdated result")


@pytest.mark.parametrize("operation", ["draw", "cancel"])
async def test_large_lottery_keeps_winner_and_refund_work_bounded(env, operation):
    _, db, _, _ = env
    from purrai.lottery import cancel, cycle, draw
    from purrai.lottery_schemas import LotteryConfig
    from purrai.models import Lottery, LotteryEntry
    config = LotteryConfig(title="Bulk settlement", prizes=[{"name": "Prize", "quantity": 2}]).model_dump()
    async with db.transaction() as session:
        lottery = Lottery(group_id=-1001, state="open", created_by=1, config=config, participant_count=2000)
        session.add(lottery)
        await session.flush()
        identifier = lottery.id
        await session.execute(PointsAccount.__table__.insert(), [dict(id=f"bulk-account-{i}", group_id=-1001,
            source_key=str(i), user_id=i + 1000, display_name=str(i), balance=0) for i in range(2000)])
        await session.execute(LotteryEntry.__table__.insert(), [dict(id=f"bulk-entry-{i:04}", lottery_id=identifier,
            group_id=-1001, user_id=i + 1000, display_name=str(i), fee=1, request_id=str(i), day="2026-11-26")
            for i in range(2000)])
    statements = []
    def counted(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.engine.sync_engine, "before_cursor_execute", counted)
    try:
        async with db.transaction() as session:
            row = await session.get(Lottery, identifier)
            await (draw(session, row) if operation == "draw" else cancel(session, row))
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", counted)
    async with db.sessions() as session:
        states = dict((await session.execute(select(LotteryEntry.state, func.count()).where(
            LotteryEntry.lottery_id == identifier).group_by(LotteryEntry.state))).all())
        if operation == "draw":
            assert states == {"won": 2, "lost": 1998}
            assert len(statements) < 35
        else:
            assert states == {"refunded": 100, "refund_pending": 1900}
            assert len(statements) < 1100
    if operation == "cancel":
        async with db.transaction() as session:
            assert await cycle(session) == 100
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 200
            assert await session.scalar(select(func.sum(PointsAccount.balance))) == 200


async def test_review_storage_caps_raw_payloads_and_keeps_recent_and_referenced_evidence(env):
    settings, db, tg, service = env
    from purrai.operational_retention import prune_operational_state, storage_status
    from purrai.review_storage import payload_size
    from purrai.models import AdvertisementSample
    settings.review_payload_max_mb = 1
    async with db.transaction() as session:
        for i in range(3):
            session.add(AdvertisementReview(id=f"cap-{i}", group_id=-1001, message_id=i, user_id=42,
                revision=str(i), message=message(i, "x" * 600000), event_at=now(), state="pending"))
    async with db.sessions() as session:
        status = await storage_status(session, settings)
        assert 0 < status["bytes"] <= 1048576 and status["limited"]["count"] == 2
        assert status["bytes"] == await session.scalar(select(func.sum(payload_size(AdvertisementReview.message))))
        assert await session.scalar(select(func.count()).select_from(AdvertisementReview).where(
            AdvertisementReview.state == "cancelled")) == 2
    async with db.transaction() as session:
        for i, age in enumerate((47 * 3600, 49 * 3600, 40 * 86400, 40 * 86400), 10):
            session.add(AdvertisementReview(id=f"age-{i}", group_id=-1001, message_id=i, user_id=42,
                revision=str(i), message=message(i, "ordinary retained message"), event_at=now() - age,
                created_at=now() - age, state="pending"))
        session.add(AdvertisementSample(group_id=-1001, text="review evidence", scope="context", source="model",
            source_key="review:age-13"))
        session.add(Action(kind="points_message", dedupe_key="old-points", group_id=-1001, user_id=42,
            state="succeeded", created_at=now() - 49 * 3600, payload={"message": message(80, "old job")}))
        session.add(Action(kind="points_message", dedupe_key="old-pending-points", group_id=-1001, user_id=43,
            state="pending", created_at=now() - 49 * 3600, payload={"message": message(81, "expired pending job")}))
        session.add(Action(kind="points_message", dedupe_key="credited-pending-points", group_id=-1001, user_id=44,
            state="pending", created_at=now() - 49 * 3600,
            payload={"message": message(82, "credited pending job"), "operation": "signin", "reply": "durable credit receipt"}))
        await session.flush()
        await prune_operational_state(session, settings)
    async with db.sessions() as session:
        assert not (await session.get(AdvertisementReview, "age-10")).message.get("_retained")
        assert (await session.get(AdvertisementReview, "age-11")).message["_retained"]
        assert await session.get(AdvertisementReview, "age-12") is None
        assert (await session.get(AdvertisementReview, "age-13")).message["_retained"]
        old = await session.scalar(select(Action).where(Action.dedupe_key == "old-points"))
        assert old.payload == {"source_expired": True}
        pending = await session.scalar(select(Action).where(Action.dedupe_key == "old-pending-points"))
        assert pending.state == "cancelled" and pending.payload == {"source_expired": True}
        credited = await session.scalar(select(Action).where(Action.dedupe_key == "credited-pending-points"))
        assert credited.state == "pending" and credited.payload["reply"] == "durable credit receipt"
        assert "message" not in credited.payload and credited.payload["reply_source"] == {"message_id": 82}
        assert (await storage_status(session, settings))["bytes"] == await session.scalar(
            select(func.sum(payload_size(AdvertisementReview.message))))
    assert await service.run_one_action(include_cleanup=False, lane="notification")
    assert any(method == "send_message" and data.get("text") == "durable credit receipt" for method, data in tg.calls)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 0
    from purrai.errors import ManagementError
    from purrai.management import Management
    management = Management(db, tg, settings=settings)
    async with db.transaction() as session:
        expired = await session.scalar(select(Action).where(Action.dedupe_key == "old-pending-points"))
        receipt = await session.scalar(select(Action).where(Action.dedupe_key == "credited-pending-points"))
        expired.state = receipt.state = "failed"
        expired_id, receipt_id = expired.id, receipt.id
    with pytest.raises(ManagementError, match="保留期限"):
        await management.retry(1, expired_id, request_id="expired-runtime-retry")
    assert (await management.retry(1, receipt_id, request_id="credited-runtime-retry"))["ok"]
    assert await service.run_one_action(include_cleanup=False, lane="notification")
    assert sum(method == "send_message" and data.get("text") == "durable credit receipt" for method, data in tg.calls) == 1


async def test_sample_postings_preserve_direct_matches_and_follow_edits(env):
    _, db, _, _ = env
    from purrai.advertisement_store import samples
    from purrai.advertisement_similarity import rank
    from purrai.advertisement_schemas import AdvertisementSettings
    from purrai.models import AdvertisementSample
    cfg = AdvertisementSettings(minimum_length=8)
    texts = ["amber river valley garden @contact", "unrelated ocean stars melody", "琥珀山谷溪流花园清晨散步"]
    async with db.transaction() as session:
        for i, text in enumerate(texts):
            session.add(AdvertisementSample(id=f"sample-{i}", group_id=-1001, text=text,
                scope="text", source="manual", source_key=str(i)))
    async with db.sessions() as session:
        bank = await samples(session, -1001)
        for text in (texts[0], "amber valley garden @another", texts[2]):
            expected = rank(text, bank, cfg)
            actual = rank(text, await samples(session, -1001, direct_text=text), cfg)
            assert expected["matched"] and actual == expected
    async with db.transaction() as session:
        row = await session.get(AdvertisementSample, "sample-0")
        row.text = "a completely changed template"
        row.version += 1
    async with db.sessions() as session:
        assert not rank(texts[0], await samples(session, -1001, direct_text=texts[0]), cfg)


def test_every_management_operation_has_a_response_contract():
    from fastapi import FastAPI
    from purrai.api import install_api
    app = FastAPI()
    install_api(app)
    schema = app.openapi()
    operations = [operation for path in schema["paths"].values() for operation in path.values()]
    assert len(operations) == 96
    for operation in operations:
        content = operation["responses"]["200"]["content"]
        assert all(value["schema"] for value in content.values())
