"""Behavioral regressions for A01–A09, including races and uncertain delivery."""

import asyncio
import hashlib
import threading

import pytest
from sqlalchemy import func, select

from conftest import drain, message
from purrai.action_delivery import uncertain_delivery
from purrai.archive import ARCHIVE_BYTES, capture, recording_status
from purrai.management import Management
from purrai.errors import ManagementError
from purrai.models import Action, Group, Inbox, Meta, Rule, Session, Verification, now
from purrai.runtime import Runtime
from purrai.schemas import RuleInput
from purrai.state_retention import prune_temporary_state


@pytest.mark.parametrize("scope", ["message", "name", "name_and_message"])
@pytest.mark.parametrize("change", ["disable", "change", "downgrade", "during_lookup"])
async def test_rule_changes_cancel_only_unsent_permanent_mutes(env, monkeypatch, scope, change):
    settings, db, tg, svc = env
    manager = Management(db, tg, settings=settings)
    rule = await manager.rule(1, -1001, RuleInput(kind="keyword", value="旧规则", scope=scope,
                                               context_value="正文", penalty="permanent_mute"))
    incoming = message(15, "正文 旧规则")
    incoming["from"]["first_name"] = "旧规则"
    await svc.message(incoming)
    async def alter():
        async with db.transaction() as session:
            row = await session.get(Rule, rule["id"])
            if change in ("disable", "during_lookup"):
                row.enabled = False
            elif change == "change":
                row.value = "完全不同"
            else:
                row.scope, row.penalty = "message", "delete"
    if change == "during_lookup":
        original = tg.call
        async def call(method, **kwargs):
            if method == "get_chat":
                await alter()
            return await original(method, **kwargs)
        monkeypatch.setattr(tg, "call", call)
    else:
        await alter()
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind.in_(("profile_mute", "rule_mute"))))
        assert action.state == "cancelled"


async def test_slow_rule_work_holds_neither_event_loop_nor_writer_and_rechecks_snapshot(env, monkeypatch):
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Rule(id="slow", group_id=-1001, kind="keyword", value="ordinary", penalty="permanent_mute"))
    entered, release = threading.Event(), threading.Event()
    from purrai import rule_evaluation
    original = rule_evaluation.evaluate_message
    first = True
    def slow(*args):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(rule_evaluation, "evaluate_message", slow)
    task = asyncio.create_task(svc.message(message(22, "ordinary")))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        async def change_policy():
            async with db.transaction() as session:
                (await session.get(Rule, "slow")).enabled = False
        await asyncio.wait_for(change_policy(), 1)
    finally:
        release.set()
    await task
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)


async def test_http_rule_replay_conflict_and_stale_edit(client, env):
    _, db, _, _ = env
    path = "/api/groups/-1001/rules"
    body = {"kind": "keyword", "value": "first", "penalty": "permanent_mute"}
    headers = {"Idempotency-Key": "rule-create-once"}
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200
    row = first.json()
    assert (await client.post(path, json=body, headers=headers)).json() == row
    assert (await client.post(path, json={**body, "value": "different"}, headers=headers)).status_code == 409
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Rule)) == 1
    update_headers = {"Idempotency-Key": "rule-disable-once", "If-Match": row["version"]}
    disabled = await client.put(path + "/" + row["id"], json={**body, "enabled": False}, headers=update_headers)
    assert disabled.status_code == 200
    stale = await client.put(path + "/" + row["id"], json={**body, "value": "stale"},
                             headers={"Idempotency-Key": "rule-stale-edit", "If-Match": row["version"]})
    assert stale.status_code == 409
    assert (await client.get(path)).json()[0]["enabled"] is False
    assert (await client.post(path, json=body)).status_code == 422


async def test_whitelist_versions_prevent_stale_creation_edit_and_removal(client):
    path = "/api/groups/-1001/whitelist"
    body = {"user_id": 42, "moderation": True, "note": "original"}
    first = (await client.put(path, json=body, headers={"Idempotency-Key": "white-create", "If-Match": "missing"})).json()
    changed = await client.put(path, json={**body, "moderation": False},
                               headers={"Idempotency-Key": "white-revoke", "If-Match": first["version"]})
    assert changed.status_code == 200
    for method, target, payload in (("PUT", path, {**body, "note": "stale"}), ("DELETE", path + "/42", {})):
        result = await client.request(method, target, json=payload,
            headers={"Idempotency-Key": "stale-white-" + method, "If-Match": first["version"]})
        assert result.status_code == 409
    assert not (await client.get(path)).json()[0]["moderation"]


async def test_healthy_read_polling_cannot_consume_write_or_health_budget(client):
    for _ in range(125):
        assert (await client.get("/api/catalog")).status_code == 200
    assert (await client.get("/health/live")).status_code == 200
    for _ in range(235):
        assert (await client.get("/api/catalog")).status_code == 200
    limited = await client.get("/api/catalog")
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
    assert (await client.patch("/api/groups/-1001/settings", json={"settings": {"max_attempts": 4}})).status_code == 200


async def test_decline_lost_receipt_ends_generation_without_replaying_new_request(env, monkeypatch):
    from conftest import FakeTelegram
    from test_join_requests import request
    settings, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "join_requests": True, "verification_enabled": False}
    event = request()
    event["date"] = now() - 300
    await svc.join_request(event, 100)
    original = FakeTelegram.call
    declines = 0
    async def call(method, **kwargs):
        nonlocal declines
        result = await original(tg, method, **kwargs)
        if method == "decline_chat_join_request":
            declines += 1
            raise ConnectionError("lost after accepting")
        return result
    monkeypatch.setattr(tg, "call", call)
    await drain(svc)
    async with db.sessions() as session:
        task = await session.scalar(select(Action).where(Action.kind == "ban"))
        row = await session.scalar(select(Verification))
        assert task.state == "failed" and uncertain_delivery(task.payload)
        assert row.state == "failed" and row.failure_action == "decline_uncertain"
        from purrai.models import Member
        assert (await session.get(Member, (-1001, 42))).request_until > now()
    await drain(svc)
    assert declines == 1


async def test_supervision_retries_and_retains_worker_error_until_shutdown(env):
    _, db, tg, svc = env
    runtime = Runtime(db, tg, svc)
    ready, parked = asyncio.Event(), asyncio.Event()
    attempts = 0
    async def run():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("startup temporarily unavailable")
        ready.set()
        await parked.wait()
    task = asyncio.create_task(runtime.workers.supervise("probe", run), name="probe")
    runtime.workers.tasks = [task]
    try:
        await asyncio.wait_for(ready.wait(), 4)
        assert attempts == 2 and runtime.ready()
        runtime.receiver.last_error = ""
        assert "startup temporarily unavailable" in runtime.status()["last_error"]
    finally:
        await runtime.stop()
    assert attempts == 2 and task.cancelled()


async def test_group_discovery_is_bounded_partial_and_identity_does_not_scan(env, client, monkeypatch):
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add_all([Group(id=-2000 - i, title=str(i), approved=True) for i in range(44)])
        session.add(Session(id=hashlib.sha256(b"regular").hexdigest(), user_id=2, csrf="csrf", expires_at=now() + 60))
    client.cookies.set("purrai_session", "regular")
    client.headers["X-CSRF-Token"] = "csrf"
    calls = 0
    async def call(method, **kwargs):
        nonlocal calls
        calls += 1
        if kwargs["chat_id"] == -1001:
            return {"status": "administrator"}
        await asyncio.sleep(30)
    monkeypatch.setattr(tg, "call", call)
    assert (await client.get("/api/me")).status_code == 200
    assert calls == 0
    result = await asyncio.wait_for(client.get("/api/groups"), 7)
    assert result.status_code == 200 and result.headers["X-PurrAI-Groups-Partial"] == "true"
    assert [row["id"] for row in result.json()] == [-1001]
    # A discovered role never authorizes a later mutation without a fresh check.
    async def revoked(method, **kwargs):
        return {"status": "member"}
    monkeypatch.setattr(tg, "call", revoked)
    assert (await client.post("/api/groups/-1001/activate")).status_code == 403


async def test_temporary_cleanup_is_bounded_and_preserves_pending_failed_and_audit(env):
    _, db, _, _ = env
    async with db.transaction() as session:
        for index in range(12):
            session.add(Meta(key=f"bot_button:old{index}", value="{}", expires_at=now() - 1))
        session.add_all([Meta(key="permanent", value="keep"), Meta(key="bot_button:fresh", value="{}", expires_at=now() + 60),
            Inbox(id=1, payload={}, state="done", completed_at=now() - 8 * 86400),
            Inbox(id=2, payload={}, state="pending"), Inbox(id=3, payload={}, state="failed"),
            Action(id="audit", dedupe_key="audit", kind="settings", state="succeeded")])
    async with db.transaction() as session:
        await prune_temporary_state(session, limit=5)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Meta).where(Meta.expires_at < now(), Meta.expires_at > 0)) == 7
        assert await session.get(Meta, "permanent") and await session.get(Meta, "bot_button:fresh")
        assert await session.get(Inbox, 1) is None
        assert await session.get(Inbox, 2) and await session.get(Inbox, 3) and await session.get(Action, "audit")


async def test_archive_limit_counts_only_retained_archive_and_not_temporary_bytes(env):
    settings, db, _, _ = env
    async with db.transaction() as session:
        session.add(Meta(key="temporary-large", value="x" * 200000))
        await capture(session, message(15, "archive"), settings=settings)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        status = await recording_status(session, group, settings)
        assert status["state"] == "active" and 0 < status["bytes"] < 200000
        (await session.get(Meta, ARCHIVE_BYTES)).value = str(settings.archive_max_db_mb * 1024 * 1024)
    async with db.sessions() as session:
        assert (await recording_status(session, await session.get(Group, -1001), settings))["state"] == "capacity_paused"


async def test_lost_mute_receipt_is_reconciled_even_when_rule_is_disabled_after_acceptance(env, monkeypatch):
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Rule(id="owned", group_id=-1001, kind="keyword", value="before", penalty="permanent_mute"))
    original = tg.call
    lose = True
    async def call(method, **kwargs):
        nonlocal lose
        result = await original(method, **kwargs)
        if method == "restrict_chat_member" and lose:
            lose = False
            async with db.transaction() as session:
                (await session.get(Rule, "owned")).enabled = False
            raise ConnectionError("accepted but receipt lost")
        return result
    monkeypatch.setattr(tg, "call", call)
    await svc.message(message(55, "before"))
    await drain(svc)
    async with db.transaction() as session:
        (await session.scalar(select(Action).where(Action.kind == "rule_mute"))).available_at = 0
    await drain(svc)
    async with db.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "rule_mute"))
        assert action.state == "succeeded"
    assert sum(method == "restrict_chat_member" for method, _ in tg.calls) == 1


async def test_shared_rule_changes_and_local_overrides_recheck_the_effective_policy(env):
    from purrai.schemas import SharedPolicyPatch
    settings, db, tg, svc = env
    manager = Management(db, tg, settings=settings)
    shared = await manager.configuration.shared(1, SharedPolicyPatch(rules=[{
        "kind": "keyword", "value": "shared", "penalty": "permanent_mute"}], request_id="shared-origin"))
    await svc.message(message(1, "shared"))
    await manager.configuration.shared(1, SharedPolicyPatch(rules=[{**shared["rules"][0], "enabled": False}], request_id="shared-disabled"))
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)


async def test_old_bot_rule_prompt_cannot_overwrite_a_newer_management_edit(env):
    from test_bot_admin import click, deliver, open_group, private, texts
    settings, db, tg, svc = env
    runtime = Runtime(db, tg, svc)
    row = await runtime.management.rule(1, -1001, RuleInput(kind="keyword", value="original"))
    await open_group(runtime, tg)
    await runtime.admin.detail(1, -1001, "rule", row["id"])
    await click(runtime, tg, "修改内容")
    await runtime.management.rule(1, -1001, RuleInput(kind="keyword", value="newest", enabled=False),
                                  row["id"], expected_version=row["version"])
    await deliver(runtime, {"message": private("stale draft")})
    assert "记录已被修改" in texts(tg)[-1]
    async with db.sessions() as session:
        current = await session.get(Rule, row["id"])
        assert current.value == "newest" and not current.enabled


async def test_rule_version_covers_inheritance_even_when_visible_fields_match(env):
    from purrai.schemas import SharedPolicyPatch
    settings, db, tg, svc = env
    manager = Management(db, tg, settings=settings)
    shared = await manager.configuration.shared(1, SharedPolicyPatch(rules=[{"kind": "keyword", "value": "same"}], request_id="version-shared"))
    identifier = shared["rules"][0]["id"]
    inherited = await manager.queries.detail(1, -1001, "rule", identifier)
    local = await manager.rule(1, -1001, RuleInput(kind="keyword", value="same"), identifier,
                               expected_version=inherited["version"])
    assert local["version"] != inherited["version"]
    with pytest.raises(ManagementError, match="记录已被修改"):
        await manager.rules.inherit(1, -1001, identifier, inherited["version"])
    restored = await manager.rules.inherit(1, -1001, identifier, local["version"])
    assert restored["inherited"] is True


async def test_disabled_rule_cannot_take_ownership_of_an_existing_permanent_mute(env):
    from purrai.models import Member
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Rule(id="second-rule", group_id=-1001, kind="keyword", value="second", penalty="permanent_mute"))
    await svc.message(message(71, "second"))
    permissions = dict.fromkeys(tg.defaults, False)
    async with db.transaction() as session:
        (await session.get(Rule, "second-rule")).enabled = False
        member = await session.get(Member, (-1001, 42))
        member.owned_restriction = {"kind": "rule", "source_action": "first-accepted-task",
                                    "permissions": permissions, "until_date": 0, "previous": {}}
    tg.members[(-1001, 42)] = {"status": "restricted", "is_member": True, "until_date": 0, **permissions}
    await drain(svc)
    async with db.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "rule_mute"))
        assert action.state == "cancelled"
        assert (await session.get(Member, (-1001, 42))).owned_restriction["source_action"] == "first-accepted-task"
