import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select, text

from conftest import drain, join, message
from purrai.app import create_app
from purrai.chat_budget import BudgetExceeded, ChatBudget
from purrai.chat_model import ChatModel, ChatModelError, ChatReply
from purrai.config import Settings
from purrai.management import Management, ManagementError
from purrai.models import Action, ArchivedMessage, ChatTurn, Group, Inbox, LoginAttempt, Meta, MessageRevision, Verification
from purrai.runtime import Runtime
from purrai.schemas import SettingsPatch
from purrai.service import enqueue
from test_chat import configured, due
from test_bot_admin import click


def bot_join(group_id):
    event = join(user_id=999)
    event["chat"]["id"] = group_id
    return {"update_id": abs(group_id), "my_chat_member": event}


async def test_private_ingestion_discovers_only_bounded_metadata_and_advances_offset(env):
    settings, db, tg, service = env
    settings.pending_group_limit = 2
    runtime = Runtime(db, tg, service)
    try:
        await runtime.persist([bot_join(-9001), bot_join(-9002), bot_join(-9003)])
        incoming = message(1, "陌生群消息", chat={"id": -9001, "type": "supergroup"})
        await runtime.persist([{"update_id": 10000, "message": incoming}])
        await service.message(incoming)
        await runtime.chat.observe(incoming)
        async with db.sessions() as session:
            assert not (await session.get(Group, -9001)).approved
            assert await session.get(Group, -9003) is None
            for model in (Inbox, ArchivedMessage, ChatTurn, MessageRevision, Action):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
            assert (await session.get(Meta, "poll_offset")).value == "10001"
        assert tg.calls == []
    finally:
        await runtime.stop()


async def test_owner_approval_through_bot_then_friend_admin_can_configure(env):
    settings, db, tg, service = env
    runtime = Runtime(db, tg, service)
    try:
        await runtime.persist([bot_join(-9001)])
        tg.members[(-9001, 7)] = {"status": "administrator"}
        with pytest.raises(ManagementError, match="超级管理员"):
            await runtime.management.group_access(7, -9001, True)
        with pytest.raises(ManagementError, match="尚未获准"):
            await runtime.management.settings_patch(7, -9001, SettingsPatch(settings={"chat_enabled": True}))
        from test_bot_admin import deliver, private
        await deliver(runtime, {"message": private("/admin")})
        await click(runtime, tg, "等待所有者批准")
        await click(runtime, tg, "批准此群接入")
        async with db.sessions() as session:
            assert (await session.get(Group, -9001)).approved
        await runtime.management.settings_patch(7, -9001, SettingsPatch(settings={"chat_enabled": True}))
        assert [g["id"] for g in await runtime.management.groups(7)] == [-9001]
        assert await runtime.management.groups(8) == []
    finally:
        await runtime.stop()


async def test_private_access_api_is_owner_only_and_revoke_cancels_tasks(env, client):
    settings, db, tg, service = env
    async with db.transaction() as session:
        action = await enqueue(session, "warn", "private:queued", -1001, 42, {"text": "old"})
    response = await client.post("/api/groups/-1001/access", json={"approved": False})
    assert response.status_code == 200 and not response.json()["approved"]
    await drain(service)
    async with db.sessions() as session:
        assert (await session.get(Action, action.id)).state == "cancelled"
    assert not any(method == "send_message" for method, _ in tg.calls)
    denied = await client.patch("/api/groups/-1001/settings", json={"settings": {"chat_enabled": True}})
    assert denied.status_code == 403
    assert (await client.post("/api/groups/-1001/access", json={"approved": True})).status_code == 200


async def test_friend_web_session_cannot_approve_any_group(env):
    import hashlib
    from purrai.models import Session, now

    settings, db, tg, _ = env
    tg.members[(-1001, 7)] = {"status": "administrator"}
    async with db.transaction() as session:
        session.add(Session(id=hashlib.sha256(b"friend").hexdigest(), user_id=7, csrf="friend-csrf", expires_at=now()+60))
        session.add(Group(id=-9001, title="未获准的群"))
    app = create_app(settings, db, tg, start_runtime=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                cookies={"purrai_session": "friend"}, headers={"X-CSRF-Token": "friend-csrf"}) as client:
        response = await client.post("/api/groups/-9001/access", json={"approved": True})
        assert response.status_code == 403
        assert (await client.get("/api/groups")).json()[0]["id"] == -1001
    async with db.sessions() as session:
        assert not (await session.get(Group, -9001)).approved
    await app.state.runtime.stop()


async def test_revocation_during_paid_request_never_sends_late_reply(env):
    chat, model = await configured(env)
    settings, db, tg, service = env
    model.release = asyncio.Event()
    await chat.observe(message(1, "柴郡喵聊聊电影"))
    await due(chat)
    task = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 1)
        await Management(db, tg, service, settings).group_access(1, -1001, False)
    finally:
        model.release.set()
        await task
        await chat.close()
    assert not any(method == "send_message" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "skipped"


async def test_revocation_during_challenge_cleans_exact_late_message(env, monkeypatch):
    settings, db, tg, service = env
    await service.member_event(join())
    await service.run_one_action(include_progress=False)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "send_photo":
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    task = asyncio.create_task(service.run_one_action(include_progress=False))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await Management(db, tg, service, settings).group_access(1, -1001, False)
    finally:
        finish.set()
        await task
    await drain(service)
    assert any(method == "delete_message" and args["message_id"] == tg.message_id for method, args in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "cancelled"


async def test_daily_budget_atomic_and_survives_restart_then_rolls_over(env, monkeypatch):
    settings, db, _, _ = env
    settings.chat_daily_calls = 1
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_enabled": True}
    budget = ChatBudget(db, settings)
    results = await asyncio.gather(budget.reserve(-1001, 41, "system", "payload"),
                                   budget.reserve(-1001, 42, "system", "payload"), return_exceptions=True)
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
    assert (await ChatBudget(db, settings).status(-1001))["group_calls"] == 1
    with pytest.raises(BudgetExceeded, match="今日"):
        await ChatBudget(db, settings).reserve(-1001, 43, "system", "payload")
    monkeypatch.setattr(ChatBudget, "day", staticmethod(lambda: "2099-01-01"))
    await budget.reserve(-1001, 43, "system", "payload")
    assert (await budget.status(-1001))["group_calls"] == 1


@pytest.mark.parametrize("scope,field,old_limit", [
    ("global", "chat_daily_calls", 500),
    ("groups", "chat_group_daily_calls", 200),
    ("users", "chat_user_daily_calls", 60),
    ("global", "chat_daily_tokens", 1000000),
    ("groups", "chat_group_daily_tokens", 400000),
    ("users", "chat_user_daily_tokens", 100000),
])
async def test_raised_budget_resumes_same_day_without_resetting_usage(env, scope, field, old_limit):
    settings, db, _, _ = env
    new_limit = Settings.model_fields[field].default
    assert new_limit == old_limit * 5
    metric = "calls" if field.endswith("calls") else "tokens"
    data = {"day": ChatBudget.day(), "global": {}, "groups": {"-1001": {}}, "users": {"41": {}}}
    counter = data[scope] if scope == "global" else data[scope]["-1001" if scope == "groups" else "41"]
    counter.update(calls=0, tokens=0)
    counter[metric] = old_limit
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_enabled": True}
        session.add(Meta(key="private:chat-budget", value=json.dumps(data)))
    setattr(settings, field, old_limit)
    with pytest.raises(BudgetExceeded, match="额度已用完"):
        await ChatBudget(db, settings).reserve(-1001, 41, "system", "payload")
    updated = settings.model_copy(update={field: new_limit})
    budget = ChatBudget(db, updated)
    reservation = await budget.reserve(-1001, 41, "system", "payload")
    await budget.settle(reservation, 17)
    async with db.sessions() as session:
        saved = json.loads((await session.get(Meta, "private:chat-budget")).value)
    actual = saved[scope] if scope == "global" else saved[scope]["-1001" if scope == "groups" else "41"]
    assert saved["day"] == data["day"]
    assert actual[metric] == old_limit + (1 if metric == "calls" else 17)
    actual[metric] = new_limit
    async with db.transaction() as session:
        (await session.get(Meta, "private:chat-budget")).value = json.dumps(saved)
    with pytest.raises(BudgetExceeded, match="额度已用完"):
        await ChatBudget(db, updated).reserve(-1001, 41, "system", "payload")


@pytest.mark.parametrize("limit", ["chat_daily_calls", "chat_group_daily_calls", "chat_user_daily_calls"])
async def test_every_silent_model_attempt_counts_and_budget_does_not_stop_moderation(env, limit):
    chat, model = await configured(env)
    settings, db, tg, service = env
    setattr(settings, limit, 2)
    model.result = None
    for number in range(1, 4):
        await chat.observe(message(number, "柴郡喵聊聊电影"))
        await due(chat)
        assert await chat.run_one()
    assert len(model.calls) == 2
    async with db.sessions() as session:
        last = await session.scalar(select(ChatTurn).order_by(ChatTurn.id.desc()))
        assert last.state == "skipped" and "额度已用完" in last.error
    async with db.transaction() as session:
        action = await enqueue(session, "delete", "private:moderation-still-works", -1001, 43, {"message_id": 44})
    await service.run_one_action(include_progress=False)
    async with db.sessions() as session:
        assert (await session.get(Action, action.id)).state == "succeeded"
    await chat.close()


async def test_user_interval_applies_when_model_stays_silent(env):
    chat, model = await configured(env)
    env[0].chat_user_interval = 5
    model.result = None
    for number in range(1, 3):
        await chat.observe(message(number, "柴郡喵聊聊电影"))
        await due(chat)
        await chat.run_one()
    assert len(model.calls) == 1
    await chat.close()


async def test_retries_and_failed_calls_keep_reserved_budget(env):
    chat, model = await configured(env)
    model.result = ChatModelError("offline timeout", retryable=True)
    await chat.observe(message(1, "柴郡喵聊聊电影"))
    for _ in range(2):
        await due(chat)
        await chat.run_one()
    status = await chat.budget.status(-1001)
    assert len(model.calls) == status["group_calls"] == 2 and status["group_tokens"] > 0
    await chat.close()


async def test_token_reservation_and_valid_usage_settlement(env):
    chat, model = await configured(env)
    model.result = ChatReply(None, None, 10)
    await chat.observe(message(1, "柴郡喵聊聊电影"))
    await due(chat)
    await chat.run_one()
    assert (await chat.budget.status(-1001))["group_tokens"] == 10
    env[0].chat_user_daily_tokens = 1000
    await chat.observe(message(2, "柴郡喵聊聊电影"))
    await due(chat)
    await chat.run_one()
    assert len(model.calls) == 1
    await chat.close()


async def test_model_output_limit_cannot_be_overridden_by_vendor_options(env):
    settings = env[0]
    settings.chat_api_extra = {"max_tokens": 1000000}
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"should_reply":true,"reply":null}'}}],
                                        "usage": {"total_tokens": 17}})

    model = ChatModel(settings, transport=httpx.MockTransport(handler))
    settings.chat_api_key = "OFFLINE_DUMMY_KEY"
    reply = await model.reply("system", "payload")
    assert requests[0]["max_tokens"] == settings.chat_max_tokens and reply.total_tokens == 17
    settings.chat_api_extra = {"max_tokens": 1000000, "max_completion_tokens": 2000000}
    await model.reply("system", "payload")
    assert "max_tokens" not in requests[1] and requests[1]["max_completion_tokens"] == settings.chat_max_tokens
    await model.close()


async def test_local_cli_approval_and_revocation_survive_reopen(tmp_path):
    import os
    import sys
    from pathlib import Path

    from purrai.db import Database

    url = f"sqlite+aiosqlite:///{tmp_path / 'cli.sqlite3'}"
    environment = {**os.environ, "PURRAI_DATABASE_URL": url, "PURRAI_BACKUP_DIR": str(tmp_path / "backups"),
                   "PURRAI_BOT_TOKEN": "", "PYTHONDONTWRITEBYTECODE": "1"}

    async def cli(*args):
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "purrai.cli", *args,
            cwd=Path(__file__).resolve().parents[1], env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, stderr.decode(errors="replace")
        return stdout.decode("utf-8")

    await cli("init-db")
    assert json.loads(await cli("groups", "--approve=-100123456"))[0]["approved"]
    assert json.loads(await cli("groups"))[0]["approved"]
    assert not json.loads(await cli("groups", "--revoke=-100123456"))[0]["approved"]
    db = Database(url)
    try:
        async with db.sessions() as session:
            row = await session.get(Group, -100123456)
            assert not row.approved and not row.enabled and not row.settings["chat_enabled"]
    finally:
        await db.close()


async def test_login_metadata_is_cached_and_anonymous_requests_are_limited(env, monkeypatch):
    settings, db, tg, _ = env
    settings.oidc_client_id, settings.oidc_client_secret, settings.session_secret = "test", "test", "x" * 40
    app = create_app(settings, db, tg, start_runtime=False)
    original = httpx.AsyncClient.get
    discoveries = []

    async def get(client, url, **kwargs):
        if str(url).startswith("https://oauth.telegram.org/"):
            discoveries.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url), json={
                "authorization_endpoint": "https://oauth.telegram.org/auth"})
        return await original(client, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        codes = [(await client.get("/auth/telegram/start")).status_code for _ in range(6)]
    assert codes == [307] * 5 + [429] and len(discoveries) == 1
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(LoginAttempt)) == 5
    await app.state.runtime.stop()


async def test_archive_caps_preserve_existing_records(env):
    settings, db, _, service = env
    settings.archive_max_revisions = 2
    incoming = message(1, "原文")
    await service.message(incoming)
    for index in range(2):
        await service.message({**incoming, "text": f"编辑{index}", "edit_date": incoming["date"] + index + 1}, edited=True)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MessageRevision)) == 2
    settings.archive_max_db_mb = 16
    async with db.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE audit_padding (payload BLOB)"))
        await connection.execute(text("INSERT INTO audit_padding VALUES (zeroblob(17825792))"))
    await service.message(message(2, "超出容量后不再归档"))
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ArchivedMessage)) == 1
        assert await session.get(Meta, "archive:paused")
    settings.archive_max_db_mb = 64  # Covers both the 17 MB database and its WAL.
    await service.message(message(3, "提高容量阈值后继续归档"))
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ArchivedMessage)) == 2
        assert await session.scalar(select(func.count()).select_from(MessageRevision)) == 3
        assert await session.get(Meta, "archive:paused") is None
