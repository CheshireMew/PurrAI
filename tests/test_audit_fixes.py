"""Regression contracts PA-01 through PA-07 from the project audit."""

import asyncio
import logging

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from conftest import drain, join, message
from purrai.app import create_app
from purrai.management import Management
from purrai.models import Action, Group, Inbox, Member, Verification, now
from purrai.runtime import Runtime
from purrai.service import enqueue


async def challenge(env):
    _, db, _, service = env
    await service.member_event(join())
    await drain(service)
    async with db.transaction() as session:
        verification = await session.scalar(select(Verification))
        # These regressions cover persisted callbacks from the old digit keyboard.
        verification.choices = []
        return verification


def callback(v, update_id=102):
    return {"update_id": update_id, "callback_query": {
        "id": f"audit-key-{update_id}", "from": {"id": 42, "is_bot": False, "first_name": "成员"},
        "chat_instance": "audit", "data": f"v:{v.id}:{v.nonce}:1",
    }}


@pytest.mark.parametrize("state", ["pending", "failed"])
@pytest.mark.parametrize("group_id", [-1001, -1002])
async def test_pa01_unrelated_member_event_never_blocks_other_group(env, state, group_id):
    v = await challenge(env)
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    event = join(user_id=43)
    event["chat"]["id"] = group_id
    if group_id != -1001:
        async with db.transaction() as session:
            session.add(Group(id=group_id, title="其他获准群", approved=True))
    await runtime.persist([{"update_id": 101, "chat_member": event}, callback(v)])
    async with db.transaction() as session:
        row = await session.get(Inbox, 101)
        row.state, row.available_at = state, now() + 60
    try:
        assert await runtime.process_one("verification")
        async with db.sessions() as session:
            assert (await session.get(Verification, v.id)).input_digits == "1"
    finally:
        await runtime.stop()


async def test_pa01_blocked_stream_cannot_hide_other_member_after_first_page(env):
    blocked = await challenge(env)
    _, db, tg, service = env
    await service.member_event(join(user_id=43))
    await drain(service)
    async with db.sessions() as session:
        other = await session.scalar(select(Verification).where(Verification.user_id == 43))
    async with db.transaction() as session:
        (await session.get(Verification, other.id)).choices = []
    runtime = Runtime(db, tg, service)
    event = join(old="member", new="left")
    event["from"].update(is_bot=False, first_name="管理员")
    key = callback(other, 302)
    key["callback_query"]["from"]["id"] = 43
    await runtime.persist([{"update_id": 101, "chat_member": event},
                           *(callback(blocked, i) for i in range(102, 302)), key])
    async with db.transaction() as session:
        (await session.get(Inbox, 101)).state = "failed"
    try:
        assert await runtime.process_one("verification")
        async with db.sessions() as session:
            assert (await session.get(Verification, other.id)).input_digits == "1"
            assert (await session.get(Verification, blocked.id)).input_digits == ""
        async with db.transaction() as session:
            row = await session.get(Inbox, 101)
            row.state, row.available_at = "pending", 0
        assert await runtime.process_one("messages")
        assert await runtime.process_one("verification")
        async with db.sessions() as session:
            row = await session.get(Verification, blocked.id)
            assert row.state == "left" and row.input_digits == ""
    finally:
        await runtime.stop()


async def test_pa02_slow_delete_does_not_block_input_or_receipt(env, monkeypatch):
    v = await challenge(env)
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    async with db.transaction() as session:
        await enqueue(session, "delete", "audit:slow-delete", -1001, 42, {"message_id": 700})
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "delete_message":
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    task = asyncio.create_task(service.run_one_action(include_progress=False))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(runtime.persist([callback(v)]), 1)
        assert await asyncio.wait_for(runtime.process_one("verification"), 1)
        async with db.sessions() as session:
            assert (await session.get(Verification, v.id)).input_digits == "1"
    finally:
        finish.set()
        await task
        await runtime.stop()


async def test_pa03_manual_mute_lost_response_is_reconciled_after_restart(env, monkeypatch):
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    action = await management.member_action(1, -1001, 43, "mute", "audit:manual-mute")
    original = tg.call
    lose = True

    async def lost(method, **kwargs):
        nonlocal lose
        result = await original(method, **kwargs)
        if method == "restrict_chat_member" and lose:
            lose = False
            raise ConnectionError("response lost after applying restriction")
        return result

    monkeypatch.setattr(tg, "call", lost)
    await service.run_one_action(include_progress=False)
    async with db.transaction() as session:
        (await session.get(Action, action["id"])).available_at = 0
    from purrai.service import Service

    restarted = Service(db, tg, settings)
    await restarted.run_one_action(include_progress=False)
    async with db.sessions() as session:
        assert (await session.get(Action, action["id"])).state == "succeeded"
        assert (await session.get(Member, (-1001, 43))).owned_restriction
    assert len([c for c in tg.calls if c[0] == "restrict_chat_member"]) == 1
    release = await management.member_action(1, -1001, 43, "unmute", "audit:manual-unmute")
    await drain(restarted)
    async with db.sessions() as session:
        assert (await session.get(Action, release["id"])).state == "succeeded"
        assert (await session.get(Member, (-1001, 43))).owned_restriction is None


async def test_pa02_late_challenge_is_cleaned_without_overwriting_reissue(env, monkeypatch):
    from purrai.schemas import ActionInput

    settings, db, tg, service = env
    await service.member_event(join())
    await service.run_one_action(include_progress=False)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "send_photo" and not entered.is_set():
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    task = asyncio.create_task(service.run_one_action(include_progress=False))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with db.sessions() as session:
            v = await session.scalar(select(Verification))
            old_nonce = v.nonce
        result = await asyncio.wait_for(Management(db, tg, service, settings).verification_action(
            1, v.id, ActionInput(action="reissue", reason="重新出题", request_id="audit:reissue")), 1)
    finally:
        finish.set()
        await task
    await drain(service)
    photos = [data for method, data in tg.calls if method == "send_photo"]
    assert len(photos) == 2
    async with db.sessions() as session:
        current = await session.get(Verification, v.id)
        assert current.nonce != old_nonce and current.message_id == tg.message_id
        assert current.state == "pending"
        assert (await session.get(Action, result["id"])).state == "succeeded"
    assert any(method == "delete_message" and data["message_id"] == tg.message_id - 1
               for method, data in tg.calls)


@pytest.mark.parametrize("change", ["rejoin", "whitelist"])
async def test_pa02_slow_lookup_rechecks_membership_and_new_whitelist(env, monkeypatch, change):
    from purrai.schemas import WhitelistInput

    settings, db, tg, service = env
    await service.member_event(join(), event_id=1)
    async with db.sessions() as session:
        old_action = await session.scalar(select(Action).where(Action.kind == "verify_mute"))
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "get_chat_member" and kwargs["user_id"] == 42 and not entered.is_set():
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    task = asyncio.create_task(service.run_one_action(include_progress=False))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "rejoin":
            await asyncio.wait_for(service.member_event(join(old="member", new="left"), event_id=2), 1)
            await asyncio.wait_for(service.member_event(join(), event_id=3), 1)
        else:
            await asyncio.wait_for(Management(db, tg, service, settings).whitelist(
                1, -1001, WhitelistInput(user_id=42), "audit:white"), 1)
    finally:
        finish.set()
        await task
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.get(Action, old_action.id)).state == "cancelled"
    await drain(service)
    assert len([1 for method, _ in tg.calls if method == "restrict_chat_member"]) == (change == "rejoin")


async def test_pa03_interrupted_mute_intent_survives_and_reconciles(env, monkeypatch):
    from purrai.service import Service

    settings, db, tg, service = env
    result = await Management(db, tg, service, settings).member_action(1, -1001, 43, "mute", "audit:interrupt")
    applied = asyncio.Event()
    original = tg.call

    async def interrupted(method, **kwargs):
        response = await original(method, **kwargs)
        if method == "restrict_chat_member":
            applied.set()
            await asyncio.Future()
        return response

    monkeypatch.setattr(tg, "call", interrupted)
    task = asyncio.create_task(service.run_one_action(include_progress=False))
    await asyncio.wait_for(applied.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with db.sessions() as session:
        action = await session.get(Action, result["id"])
        member = await session.get(Member, (-1001, 43))
        assert action.state == "pending" and member.owned_restriction["source_action"] == action.id
    monkeypatch.setattr(tg, "call", original)
    await Service(db, tg, settings).run_one_action(include_progress=False)
    async with db.sessions() as session:
        assert (await session.get(Action, result["id"])).state == "succeeded"
    assert len([1 for method, _ in tg.calls if method == "restrict_chat_member"]) == 1


async def test_pa04_partial_web_settings_preserve_bot_changes_and_reject_conflicts(env, client):
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    await management.setting(1, -1001, "media_min_text_messages", 9, "audit:bot-setting")
    response = await client.patch("/api/groups/-1001/settings", json={
        "settings": {"chat_persona": "新的性格"}, "baseline": {"chat_persona": "随和、有一点幽默，关心群友，有自己的看法。"},
    })
    assert response.status_code == 200
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["media_min_text_messages"] == 9
    response = await client.patch("/api/groups/-1001/settings", json={
        "settings": {"media_min_text_messages": 4}, "baseline": {"media_min_text_messages": 3},
    })
    assert response.status_code == 409
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["media_min_text_messages"] == 9


@pytest.mark.parametrize("state,label", [("pending", "等待处理"), ("succeeded", "已完成"),
                                         ("failed", "失败"), ("cancelled", "已取消")])
async def test_pa05_violation_api_shows_real_profile_mute_result(env, client, monkeypatch, state, label):
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    from purrai.schemas import RuleInput

    await management.rule(1, -1001, RuleInput(kind="keyword", value="广告", scope="name"))
    incoming = message(800, "你好")
    incoming["from"]["first_name"] = "广告成员"
    await service.message(incoming)
    if state == "cancelled":
        from purrai.schemas import WhitelistInput

        await management.whitelist(1, -1001, WhitelistInput(user_id=42, moderation=True))
    elif state == "failed":
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import RestrictChatMember

        original = tg.call

        async def denied(method, **kwargs):
            if method == "restrict_chat_member":
                raise TelegramBadRequest(method=RestrictChatMember(**kwargs), message="audit permission denied")
            return await original(method, **kwargs)

        monkeypatch.setattr(tg, "call", denied)
    if state != "pending":
        await drain(service)
    response = await client.get("/api/groups/-1001/violations")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert any(a["kind"] == "profile_mute" and a["state"] == state for a in item["actions"])
    runtime = Runtime(db, tg, service)
    try:
        await runtime.admin.detail(1, -1001, "violation", item["id"])
        assert any("昵称命中永久禁言：" + label in args.get("text", "") for _, args in tg.calls)
    finally:
        await runtime.stop()


async def test_pa05_emoji_mute_is_visible_in_both_views(env, client, monkeypatch):
    from test_profile_trust import profile_api

    _, db, tg, service = env
    await profile_api(tg, monkeypatch)
    incoming = message(801, "普通消息")
    incoming["from"].update(is_premium=True, first_name="普通成员")
    await service.message(incoming)
    await drain(service)
    response = await client.get("/api/groups/-1001/violations")
    item = next(row for row in response.json()["items"]
                if any(a["kind"] == "emoji_status_mute" for a in row["actions"]))
    assert any(a["kind"] == "emoji_status_mute" and a["state"] == "succeeded" for a in item["actions"])
    runtime = Runtime(db, tg, service)
    try:
        await runtime.admin.detail(1, -1001, "violation", item["id"])
        assert any("状态表情命中永久禁言：已完成" in args.get("text", "") for _, args in tg.calls)
    finally:
        await runtime.stop()


@pytest.mark.parametrize("kind,method", [("mute", "restrict_chat_member"), ("ban", "ban_chat_member")])
async def test_pa03_expired_lost_restriction_is_never_applied_again(env, monkeypatch, kind, method):
    settings, db, tg, service = env
    result = await Management(db, tg, service, settings).member_action(1, -1001, 43, kind, "audit:expire")
    original = tg.call

    async def lost(name, **kwargs):
        response = await original(name, **kwargs)
        if name == method:
            raise ConnectionError("audit response lost")
        return response

    monkeypatch.setattr(tg, "call", lost)
    await service.run_one_action(include_progress=False)
    async with db.transaction() as session:
        row = await session.get(Member, (-1001, 43))
        expired = row.owned_restriction["until_date"] + 1
        (await session.get(Action, result["id"])).available_at = 0
    tg.members[(-1001, 43)] = {"status": "member" if kind == "mute" else "left"}
    monkeypatch.setattr("purrai.restriction_actions.now", lambda: expired)
    await service.run_one_action(include_progress=False)
    async with db.sessions() as session:
        action = await session.get(Action, result["id"])
        assert action.state == "succeeded" and action.payload["outcome"] == "restriction_expired"
        assert (await session.get(Member, (-1001, 43))).owned_restriction is None
    assert len([1 for name, _ in tg.calls if name == method]) == 1


async def test_pa06_missing_runtime_table_is_not_ready_and_startup_refuses(env):
    settings, db, tg, _ = env
    settings.bot_token = "999:OFFLINE_TEST_ONLY"
    async with db.engine.begin() as connection:
        await connection.execute(text("ALTER TABLE chat_turns RENAME TO audit_unavailable_chat_turns"))
    app = create_app(settings, db, tg, start_runtime=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://audit") as client:
        result = await client.get("/health/ready")
        assert result.status_code == 503
        assert result.json()["database_ready"] is False
    with pytest.raises(RuntimeError, match="init-db"):
        async with app.router.lifespan_context(app):
            pytest.fail("incompatible database entered application lifespan")
    await app.state.runtime.stop()


@pytest.mark.parametrize("damage", ["none", "revision", "column"])
async def test_pa06_schema_version_and_columns_match_startup_and_readiness(env, damage):
    settings, db, tg, _ = env
    settings.bot_token = "999:OFFLINE_TEST_ONLY"
    async with db.engine.begin() as connection:
        if damage == "revision":
            await connection.execute(text("UPDATE alembic_version SET version_num='0005'"))
        elif damage == "column":
            await connection.execute(text("ALTER TABLE chat_turns RENAME COLUMN response TO audit_response"))
    app = create_app(settings, db, tg, start_runtime=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://audit") as client:
        result = await client.get("/health/ready")
        assert result.status_code == (200 if damage == "none" else 503)
        assert result.json()["database_ready"] == (damage == "none")
    if damage == "none":
        async with app.router.lifespan_context(app):
            pass
    else:
        with pytest.raises(RuntimeError, match="init-db"):
            async with app.router.lifespan_context(app):
                pytest.fail("incompatible database started")
    await app.state.runtime.stop()


async def test_pa06_worker_exit_is_not_ready_and_keeps_diagnostic(env, monkeypatch, caplog):
    settings, db, tg, _ = env
    settings.bot_token = "999:OFFLINE_TEST_ONLY"
    app = create_app(settings, db, tg, start_runtime=True)
    runtime = app.state.runtime

    async def idle(*args):
        await asyncio.Future()

    async def broken():
        raise ValueError("audit startup recovery failed")

    for owner, name in ((runtime, "poll_loop"), (runtime, "inbox_loop"),
                        (runtime.service, "action_loop"), (runtime.service.progress, "loop"),
                        (runtime.service.emoji_status, "loop")):
        monkeypatch.setattr(owner, name, idle)
    monkeypatch.setattr(runtime.chat, "run", broken)
    try:
        with caplog.at_level(logging.ERROR):
            runtime.start()
            await asyncio.wait(runtime.tasks, timeout=1, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(0)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://audit") as client:
            result = await client.get("/health/ready")
            assert result.status_code == 503 and result.json()["runtime_ready"] is False
        assert "worker=chat" in caplog.text and "audit startup recovery failed" in runtime.last_error
    finally:
        await runtime.stop()


async def test_pa07_failed_inbox_has_correlated_sanitized_diagnostic(env, monkeypatch, caplog):
    settings, db, tg, service = env
    settings.chat_api_key = "audit-private-api-key"
    runtime = Runtime(db, tg, service)
    incoming = message(900, "普通消息")
    incoming["from"].update(is_bot=False, first_name="成员")
    await runtime.persist([{"update_id": 901, "message": incoming}])

    async def broken(*args, **kwargs):
        raise ValueError(f"audit diagnostic cause {settings.chat_api_key}")

    monkeypatch.setattr(service, "message", broken)
    try:
        with caplog.at_level(logging.WARNING):
            assert await runtime.process_one("messages")
        assert "audit diagnostic cause" in caplog.text
        assert settings.chat_api_key not in caplog.text
        assert "901" in caplog.text and "ValueError" in caplog.text
        async with db.sessions() as session:
            row = await session.get(Inbox, 901)
            assert "901" in row.error
            assert settings.chat_api_key not in row.error
    finally:
        await runtime.stop()


@pytest.mark.parametrize("stage", ["action", "progress", "emoji", "chat"])
async def test_pa07_worker_records_keep_safe_cause_and_identity(env, monkeypatch, caplog, stage):
    settings, db, tg, service = env
    settings.chat_api_key = "audit-private-worker-key"
    if stage == "progress":
        v = await challenge(env)
        await service.callback(callback(v)["callback_query"])
        async with db.transaction() as session:
            row = await session.scalar(select(Action).where(Action.kind == "progress"))
            row.available_at = 0
        target = "edit_message_caption"
    elif stage == "action":
        async with db.transaction() as session:
            row = await enqueue(session, "warn", "audit:diagnostic", -1001, 42, {"text": "test"})
        target = "send_message"
    elif stage == "emoji":
        async with db.transaction() as session:
            group = await session.get(Group, -1001)
            await service.emoji_status.queue(session, group, {"id": 42, "is_premium": True})
            row = await session.scalar(select(Action).where(Action.kind == "emoji_status_check"))
        target = "get_chat"
    else:
        from purrai.models import ChatTurn
        from test_chat import configured, due

        chat, model = await configured(env)
        model.result = ValueError(f"audit chat cause {settings.chat_api_key}")
        await chat.observe(message(910, "柴郡喵聊聊电影"))
        await due(chat)
        with caplog.at_level(logging.WARNING):
            assert await chat.run_one()
        async with db.sessions() as session:
            row = await session.scalar(select(ChatTurn))
            assert row.state == "failed" and str(row.id) in row.error
            assert "audit chat cause" in row.error and settings.chat_api_key not in row.error
        assert settings.chat_api_key not in caplog.text and "ValueError" in caplog.text
        await chat.close()
        return

    identifier = row.id
    original = tg.call

    async def broken(method, **kwargs):
        if method == target:
            raise ConnectionError(f"audit {stage} cause {settings.chat_api_key}")
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", broken)
    with caplog.at_level(logging.WARNING):
        if stage == "action":
            assert await service.run_one_action(include_progress=False)
        else:
            worker = service.progress if stage == "progress" else service.emoji_status
            assert await worker.run_one()
    async with db.sessions() as session:
        row = await session.get(Action, identifier)
        assert f"audit {stage} cause" in row.error and identifier in row.error
        assert settings.chat_api_key not in row.error
    assert identifier in caplog.text and "ConnectionError" in caplog.text
    assert settings.chat_api_key not in caplog.text
