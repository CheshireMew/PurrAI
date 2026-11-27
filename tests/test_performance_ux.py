"""Task ordering, bounded reads and committed button results under slow I/O."""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import event, insert, select

from purrai.action_queue import enqueue
from purrai.models import Action, Group, Inbox, Meta, PointsPolicy, Violation, now
from purrai.points_policy import default_policy
from purrai.runtime import Runtime
from purrai.serialization import row_dict


async def test_due_task_uses_one_query_past_five_thousand_future_tasks(env):
    _, db, _, service = env
    async with db.transaction() as session:
        await session.execute(insert(Action), [{"id": f"future-{i}", "dedupe_key": f"future-{i}",
            "kind": "unmute", "group_id": -1001, "user_id": i + 1, "state": "pending",
            "created_at": 1, "available_at": now() + 86400, "payload": {"automatic_restore": True}}
            for i in range(5000)])
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)
    event.listen(db.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await service.actions.claim_action(include_progress=False, cleanup_only=False, lane="member") is None
        assert len(queries) == 1
        async with db.transaction() as session:
            task = await enqueue(session, "manual_mute", "due-task", -1001, 42)
            identifier = task.id
        queries.clear()
        result = await service.actions.claim_action(include_progress=False, cleanup_only=False, lane="member")
        assert result[0] == identifier and len(queries) == 1
        service.action_claims.release(result[0], result[2])
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("kind,payload,lane", [
    ("manual_mute", {}, "member"), ("auto_reply", {}, "notification"),
    ("points_message", {"operation": "chat"}, "notification"), ("group_menu", {}, "notification"),
])
async def test_delayed_head_preserves_its_scope_and_other_scopes_remain_runnable(env, kind, payload, lane):
    _, db, _, service = env
    async with db.transaction() as session:
        first = await enqueue(session, kind, "first", -1001, 42, payload)
        first.available_at = now() + 3600
        second = await enqueue(session, kind, "second", -1001, 42, payload)
        await session.flush()
        async with session.begin_nested():
            session.add(Group(id=-2000, approved=True))
        other = await enqueue(session, kind, "other", -2000 if kind == "group_menu" else -1001, 43, payload)
        expected, follower = other.id, second.id
    claim = await service.actions.claim_action(include_progress=False, cleanup_only=False, lane=lane)
    assert claim[0] == expected
    assert await service.actions.claim_action(include_progress=False, cleanup_only=False, lane=lane) is None
    service.action_claims.release(claim[0], claim[2])
    async with db.transaction() as session:
        (await session.get(Action, expected)).state = "succeeded"
        (await session.get(Action, first.id)).state = "cancelled"
    claim = await service.actions.claim_action(include_progress=False, cleanup_only=False, lane=lane)
    assert claim[0] == follower
    service.action_claims.release(claim[0], claim[2])


async def test_scope_updates_and_equal_timestamp_order_keep_original_semantics(env):
    _, db, _, service = env
    async with db.transaction() as session:
        first = Action(id="z-first", dedupe_key="equal-first", group_id=-1001, user_id=42,
                       kind="manual_mute", created_at=100, available_at=0, priority=10)
        session.add(first)
        await session.flush()
        session.add(Action(id="a-second", dedupe_key="equal-second", group_id=-1001, user_id=42,
                           kind="manual_mute", created_at=100, available_at=0, priority=10))
        notification = await enqueue(session, "points_message", "changing-scope", -1001, 43, {"operation": "chat"})
        notification.payload = {"operation": "signin"}
        await session.flush()
        assert json.loads(notification.queue_scope) == ["notification", -1001, 43]
        assert "queue_scope" not in row_dict(notification)
    claim = await service.actions.claim_action(include_progress=False, cleanup_only=False, lane="member")
    assert claim[0] == "z-first"
    assert await service.actions.claim_action(include_progress=False, cleanup_only=False, lane="member") is None
    service.action_claims.release(claim[0], claim[2])


async def test_violation_projection_batches_associations_and_preserves_old_evidence(client, env):
    _, db, _, _ = env
    async with db.transaction() as session:
        await session.execute(insert(Action), [{"id": f"history-{i}", "dedupe_key": f"history-{i}",
            "kind": "points_message", "group_id": -1001, "user_id": 42,
            "state": "succeeded", "payload": {}} for i in range(2000)])
        for i in range(20):
            session.add(Violation(id=f"violation-{i}", group_id=-1001, message_id=-(i + 1), user_id=i + 1,
                                  reasons=[], created_at=i))
            await enqueue(session, "delete", f"penalty-{i}", -1001, i + 1, {"violation_id": f"violation-{i}"})
        await enqueue(session, "warn", "warning:violation-0", -1001, 1)
        session.add(Meta(key="violation-correction:violation-0", value=json.dumps({"actor_id": 1, "reason": "Reviewed"})))
        session.add(Meta(key="group-migration:-1001", value=json.dumps({"old_id": -999, "new_id": -1001})))
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)
    event.listen(db.engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await client._transport.app.state.management.queries.page(1, -1001, "violation")
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", capture)
    assert len(queries) <= 5
    rows = {row["id"]: row for row in result["items"]}
    assert rows["violation-0"]["correction"]["reason"] == "Reviewed"
    assert len(rows["violation-0"]["actions"]) == 2
    assert all(len(row["actions"]) == 1 for key, row in rows.items() if key != "violation-0")
    assert all(row["origin_group_id"] == -999 and row["origin_message_id"] > 0 for row in rows.values())
    async with db.engine.connect() as connection:
        plan = await connection.exec_driver_sql("EXPLAIN QUERY PLAN SELECT id FROM actions WHERE group_id=-1001 AND json_extract(payload, '$.violation_id')='violation-0'")
        assert "ix_actions_violation" in str(plan.all())


@pytest.mark.parametrize("feature", ["ranking", "lottery"])
async def test_slow_button_hint_cannot_hold_inbox_or_repeat_committed_result(env, monkeypatch, feature):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    entered, release = asyncio.Event(), asyncio.Event()
    original = tg.call
    async def slow(method, **kwargs):
        if method == "answer_callback_query":
            assert kwargs["request_timeout"] == 3
            entered.set()
            await release.wait()
        return await original(method, **kwargs)
    monkeypatch.setattr(tg, "call", slow)
    policy = default_policy().model_dump()
    policy["enabled"] = True
    async with db.transaction() as session:
        session.add(PointsPolicy(group_id=-1001, config=policy))
    if feature == "lottery":
        # Use the same real management operation as the API, without HTTP login setup.
        from purrai.lottery_schemas import LotteryConfig, LotterySave, LotteryOperation
        row = await runtime.management.lotteries.save(1, -1001,
            LotterySave(config=LotteryConfig(title="Callback test", prizes=[{"name": "Prize", "kind": "manual", "quantity": 1}])) , "slow-create")
        row = await runtime.management.lotteries.operate(1, -1001, row["id"],
            LotteryOperation(operation="publish", version=row["version"]), "slow-publish")
        data = f"lot:join:{row['id']}"
    else:
        data = "pts:rank:1"
    query = {"id": "slow-hint", "chat_instance": "test", "from": {"id": 42, "is_bot": False, "first_name": "Test"},
             "message": {"message_id": 99, "date": int(now()), "chat": {"id": -1001, "type": "supergroup"}}, "data": data}
    try:
        await runtime.persist([{"update_id": 600, "callback_query": query}])
        assert await asyncio.wait_for(runtime.process_one("messages"), 1)
        await asyncio.wait_for(entered.wait(), 1)
        async with db.sessions() as session:
            assert (await session.get(Inbox, 600)).state == "done"
        assert not runtime.inbox.claims.scopes and len(runtime.events.ack_tasks) == 1
        await runtime.persist([{"update_id": 601, "callback_query": {**query, "id": "next-hint"}}])
        assert await asyncio.wait_for(runtime.process_one("messages"), 1)
        if feature == "lottery":
            from purrai.models import LotteryEntry
            async with db.sessions() as session:
                assert len(list(await session.scalars(select(LotteryEntry)))) == 1
    finally:
        await runtime.stop()
    assert not runtime.events.ack_tasks


async def test_points_status_reports_local_wait_and_only_current_group_pending_chat(client, env):
    settings, db, _, _ = env
    config = default_policy().model_dump()
    config["enabled"] = True
    async with db.transaction() as session:
        session.add(PointsPolicy(group_id=-1001, config=config))
        for kind, operation, state, group_id in [
            ("points_message", "chat", "pending", -1001), ("points_message", "chat", "succeeded", -1001),
            ("points_message", "signin", "pending", -1001), ("points_message", "chat", "pending", -2000),
        ]:
            job = await enqueue(session, kind, f"status-{operation}-{state}-{group_id}", group_id, 42, {"operation": operation})
            job.state = state
    result = (await client.get("/api/groups/-1001/points")).json()["settlement"]
    assert result["mode"] == "local" and result["pending_messages"] == 1
    assert result["local_window_hours"] == 47 and result["oldest_at"] > 0
    settings.chat_api_key = "offline-test-key"
    result = (await client.get("/api/groups/-1001/points")).json()["settlement"]
    assert result["mode"] == "model"
    async with db.transaction() as session:
        for job in await session.scalars(select(Action).where(Action.group_id == -1001)):
            job.state = "succeeded"
    result = (await client.get("/api/groups/-1001/points")).json()["settlement"]
    assert result["pending_messages"] == 0 and result["oldest_at"] is None


def test_action_index_migration_preserves_pending_work_and_old_associations(tmp_path):
    database = tmp_path / "upgrade.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}", "PYTHONUTF8": "1"}
    root = Path(__file__).resolve().parents[1]
    for revision in ("0039", "0040"):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision], cwd=root,
                                env=environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        with sqlite3.connect(database) as connection:
            if revision == "0039":
                connection.execute("INSERT INTO actions (id,dedupe_key,kind,state,group_id,user_id,payload,priority,attempts,available_at,error,reason,created_at,updated_at) VALUES ('old','old','manual_mute','pending',-1001,42,?,10,0,9999999999,'','old reason',100,100)",
                                   (json.dumps({"violation_id": "original"}),))
            else:
                assert connection.execute("SELECT state,reason,created_at,payload,queue_scope FROM actions WHERE id='old'").fetchone() == (
                    "pending", "old reason", 100, json.dumps({"violation_id": "original"}), '["member",-1001,42]')
                assert {"ix_actions_due", "ix_actions_scope_order", "ix_actions_violation", "ix_points_pending"} <= {
                    row[1] for row in connection.execute("PRAGMA index_list(actions)")}
