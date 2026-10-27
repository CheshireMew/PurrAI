"""Regression contracts for PA-01 through PA-07, using the formal services."""

import copy
import asyncio
import json
import logging
import hashlib

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import event, select

from conftest import drain, message
from purrai.action_queue import enqueue
from purrai.app import create_app
from purrai.management import Management
from purrai.errors import ManagementError
from purrai.models import Action, ChatTurn, Group, now
from purrai.schemas import ActionInput
from purrai.service import Service
from test_chat import configured
from test_join_requests import prepare, request
from test_verification import latest
from test_bot_admin import click, open_group, markup
from purrai.runtime import Runtime


@pytest.mark.parametrize("reissue", [False, True], ids=["initial", "manual-reissue"])
async def test_join_request_lost_photo_receipt_recovers_after_restart(env, monkeypatch, reissue):
    runtime = await prepare(env, monkeypatch)
    settings, db, tg, service = env
    await service.join_request(request(), event_id=1)
    if reissue:
        await drain(service)
        old = await latest(db)
        await runtime.management.verification_action(
            1, old.id, ActionInput(action="reissue", reason="重新出题", request_id="lost-reissue")
        )
    original, accepted = tg.call, []

    async def lose(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "send_photo":
            accepted.append((kwargs, result["message_id"]))
            raise ConnectionError("response lost after acceptance")
        return result

    monkeypatch.setattr(tg, "call", lose)
    await drain(service)
    verification = await latest(db)
    assert verification.message_id is None and len(accepted) == 1
    restarted = Service(db, tg, settings)
    assert not await restarted.run_one_action()
    data, identifier = accepted[0]
    # Obtain the current button from the actual outbound image, not a predicted nonce.
    correct = next(button["callback_data"] for row in data["reply_markup"]["inline_keyboard"]
                   for button in row if button["text"] == "123456")
    if reissue:
        original_photo = next(values for method, values in tg.calls if method == "send_photo")
        old_button = original_photo["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert "失效" in await restarted.callback({
            "id": "old-picture", "from": {"id": old.user_id}, "data": old_button,
            "message": {"message_id": old.message_id, "chat": {"id": original_photo["chat_id"]}},
        })
    answer = await restarted.callback({
        "id": "receipt-recovery", "from": {"id": verification.user_id}, "data": correct,
        "message": {"message_id": identifier, "chat": {"id": data["chat_id"]}, "from": {"id": tg.user_id}},
    })
    assert "答案正确" in answer
    await drain(restarted)
    assert (await latest(db)).state == "passed"
    assert any(method == "delete_message" and values["message_id"] == identifier
               and values["chat_id"] == data["chat_id"] for method, values in tg.calls)
    assert len(accepted) == 1


@pytest.mark.parametrize("kind,method", [
    ("admin_notify", "send_message"), ("warn", "send_message"),
    ("archive_export", "send_document"), ("join_link", "create_chat_invite_link"),
])
async def test_uncertain_notification_never_repeats_after_restart_or_manual_retry(env, monkeypatch, kind, method):
    settings, db, tg, service = env
    await service.message(message(501, "export sample"))
    async with db.transaction() as session:
        task = await enqueue(session, kind, f"delivery:{kind}", -1001, actor_id=1,
                             payload={"action_ids": [], "chat_id": 1, "text": "notice",
                                      "filters": {}, "cutoff": now()})
        identifier = task.id
    original, accepted = tg.call, []

    async def lose(operation, **kwargs):
        result = await original(operation, **kwargs)
        if operation == method:
            accepted.append(result)
            raise ConnectionError("response lost after acceptance")
        return result

    monkeypatch.setattr(tg, "call", lose)
    assert await service.run_one_action(include_progress=False)
    async with db.transaction() as session:
        task = await session.get(Action, identifier)
        assert task.state == "failed" and "结果未确认" in task.error
        task.available_at = 0
    restarted = Service(db, tg, settings)
    assert not await restarted.run_one_action(include_progress=False)
    with pytest.raises(ManagementError, match="结果未确认"):
        await Management(db, tg, settings=settings).retry(1, identifier, request_id="retry-unknown")
    assert len(accepted) == 1


async def test_reply_recovery_work_is_linear_and_drains_tail(env, record_property):
    chat, _ = await configured(env)
    counts = []
    completed = 0
    for count in (10, 20, 105):
        # Create real reply receipts through observation/generation/delivery.
        start = count * 100
        for index in range(count - completed):
            await chat.observe(message(start + index, "柴郡喵，聊聊周末"))
            await chat.run_one()
        async with chat.db.transaction() as session:
            turns = (await session.scalars(select(ChatTurn).where(ChatTurn.message_id >= start))).all()
            for turn in turns:
                saved = json.loads(turn.response)
                turn.response = json.dumps({**saved, "source_ids": [99999]})
            task = await enqueue(session, "delete", "shared-deleted-source", -1001, payload={"message_id": 99999})
            task.state = "succeeded"
        statements = []

        def record(*args):
            statements.append(args[2])

        event.listen(chat.db.engine.sync_engine, "before_cursor_execute", record)
        try:
            await chat.recover()
        finally:
            event.remove(chat.db.engine.sync_engine, "before_cursor_execute", record)
        counts.append(len(statements))
        completed = count
    assert counts[1] <= counts[0] * 2.7, counts
    # Each turn may read its sources/legacy receipt, reconcile one delete and
    # record its outcome. The bound includes fresh writes and batch overhead.
    assert all(total <= size * 12 + 20 for size, total in zip((10, 20, 105), counts)), counts
    record_property("recovery_sql_10_20_105", counts)
    while await env[3].run_one_cleanup():
        pass
    async with chat.db.sessions() as session:
        turns = (await session.scalars(select(ChatTurn))).all()
        assert len(turns) == 105
        assert all(turn.state == "retracted" for turn in turns)
        identifiers = {turn.sent_message_id for turn in turns}
    assert identifiers <= {values["message_id"] for method, values in chat.tg.calls if method == "delete_message"}


async def test_group_admin_archive_reports_live_capacity_state(env, client):
    settings, db, tg, service = env
    # A group administrator, without superadministrator access, consumes the normal API.
    settings.superadmin_ids = []
    tg.members[(-1001, 1)] = {"status": "administrator"}
    response = await client.get("/api/groups/-1001/messages")
    assert response.status_code == 200
    assert response.json()["recording"]["state"] == "active"
    settings.archive_max_db_mb = 0
    await service.message(message(600, "capacity-paused"))
    response = await client.get("/api/groups/-1001/messages")
    assert response.json()["recording"]["state"] == "capacity_paused"
    assert "容量" in response.json()["recording"]["reason"]
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, "消息归档与导出")
    assert "容量不足，已暂停记录" in markup(tg)["text"]
    settings.archive_max_db_mb = 512
    assert (await client.get("/api/groups/-1001/messages")).json()["recording"]["state"] == "active"
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "archive_enabled": False}
    assert (await client.get("/api/groups/-1001/messages")).json()["recording"]["state"] == "disabled"


async def test_cli_run_emits_correlated_chat_events(env, monkeypatch, capsys):
    import sys
    import uvicorn
    from purrai import cli

    chat, _ = await configured(env)
    captured = {}

    def run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(sys, "argv", ["purrai", "run"])
    monkeypatch.setattr(cli, "Settings", lambda: env[0])
    monkeypatch.setattr("purrai.app.create_app", lambda settings: object())
    monkeypatch.setattr(uvicorn, "run", run)
    cli.main()
    from logging.config import dictConfig
    from uvicorn.config import LOGGING_CONFIG
    # Use precisely the configuration chosen by the supported CLI entry.
    saved_handlers = logging.getLogger("purrai").handlers[:]
    saved_level = logging.getLogger("purrai").level
    saved_propagate = logging.getLogger("purrai").propagate
    try:
        dictConfig(copy.deepcopy(captured.get("log_config", LOGGING_CONFIG)))
        await chat.observe(message(700, "柴郡喵，聊聊周末"))
        await chat.run_one()
        logging.getLogger("purrai").info("diagnostic credential=%s", env[0].chat_api_key)
        output = capsys.readouterr().err
        assert "chat" in output and "turn=" in output and "group=-1001" in output
        assert "聊天选择" in output and "聊天请求模型" in output and "聊天发送结束" in output
        assert "聊聊周末" not in output and "secret" not in output
    finally:
        logger = logging.getLogger("purrai")
        logger.handlers, logger.level = saved_handlers, saved_level
        logger.propagate = saved_propagate


async def test_static_entry_rejects_unidentified_old_build(env, tmp_path):
    from httpx import ASGITransport, AsyncClient
    settings, db, tg, _ = env
    settings.frontend_dist = tmp_path / "old-dist"
    settings.frontend_dist.mkdir()
    (settings.frontend_dist / "index.html").write_text("<p>obsolete editor</p>", encoding="utf-8")
    app = create_app(settings, db, tg, start_runtime=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as consumer:
        result = await consumer.get("/")
        assert result.status_code == 503 and "构建" in result.text


@pytest.mark.parametrize("kind,method", [
    ("admin_notify", "send_message"), ("warn", "send_message"),
    ("archive_export", "send_document"), ("join_link", "create_chat_invite_link"),
])
@pytest.mark.parametrize("rejection", ["rate-limit", "bad-request"])
async def test_explicit_notification_rejection_can_retry(env, monkeypatch, kind, method, rejection):
    settings, db, tg, service = env
    await service.message(message(800, "exportable"))
    async with db.transaction() as session:
        task = await enqueue(session, kind, f"rejected:{kind}", -1001, actor_id=1,
                             payload={"action_ids": [], "chat_id": 1, "text": "notice", "filters": {}, "cutoff": now()})
        identifier = task.id
    original, attempts = tg.call, 0

    async def reject(operation, **kwargs):
        nonlocal attempts
        if operation == method:
            attempts += 1
            if attempts == 1:
                request = SendMessage(chat_id=1, text="metadata")
                if rejection == "rate-limit":
                    raise TelegramRetryAfter(method=request, message="retry", retry_after=1)
                raise TelegramBadRequest(method=request, message="rejected before acceptance")
        return await original(operation, **kwargs)

    monkeypatch.setattr(tg, "call", reject)
    assert await service.run_one_action(include_progress=False)
    async with db.transaction() as session:
        task = await session.get(Action, identifier)
        assert task.payload["delivery"]["state"] == "rejected"
        task.available_at = 0
    if rejection == "bad-request":
        await Management(db, tg, settings=settings).retry(1, identifier, request_id="explicit-retry")
    assert await Service(db, tg, settings).run_one_action(include_progress=False)
    async with db.sessions() as session:
        task = await session.get(Action, identifier)
        assert task.state == "succeeded" and task.payload["delivery"]["state"] == "confirmed"
        if kind == "join_link":
            assert task.payload["invite_link"] == task.payload["delivery"]["result"]["invite_link"]
    assert attempts == 2


@pytest.mark.parametrize("kind,method", [
    ("admin_notify", "send_message"), ("warn", "send_message"),
    ("archive_export", "send_document"), ("join_link", "create_chat_invite_link"),
])
async def test_interrupted_notification_does_not_repeat_after_restart(env, monkeypatch, kind, method):
    settings, db, tg, service = env
    await service.message(message(900, "exportable"))
    async with db.transaction() as session:
        task = await enqueue(session, kind, f"interrupted:{kind}", -1001, actor_id=1,
                             payload={"action_ids": [], "chat_id": 1, "text": "notice", "filters": {}, "cutoff": now()})
        identifier = task.id
    original, accepted = tg.call, []

    async def interrupt(operation, **kwargs):
        result = await original(operation, **kwargs)
        if operation == method:
            accepted.append(result)
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(tg, "call", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await service.run_one_action(include_progress=False)
    async with db.sessions() as session:
        assert (await session.get(Action, identifier)).payload["delivery"]["state"] == "sending"
    assert await Service(db, tg, settings).run_one_action(include_progress=False)
    async with db.sessions() as session:
        task = await session.get(Action, identifier)
        assert task.state == "failed" and "结果未确认" in task.error
    assert len(accepted) == 1


def test_frontend_build_invalidates_changed_input_output_and_source_set(tmp_path):
    from purrai.frontend_build import FrontendBuild, INPUTS

    source, dist = tmp_path / "frontend", tmp_path / "dist"
    (source / "src").mkdir(parents=True)
    (dist / "assets").mkdir(parents=True)
    for name in (*INPUTS, "src/main.ts"):
        (source / name).write_text("source:" + name, encoding="utf-8")
    (dist / "index.html").write_text('<script src="/assets/current.js"></script>', encoding="utf-8")
    (dist / "assets/current.js").write_text("current", encoding="utf-8")
    (dist / "assets/old.js").write_text("obsolete", encoding="utf-8")
    def digest(file):
        return hashlib.sha256(file.read_bytes()).hexdigest()
    manifest = {"schema": 1, "inputs": {name: digest(source / name) for name in (*INPUTS, "src/main.ts")},
                "outputs": {name: digest(dist / name) for name in ("index.html", "assets/current.js")}}
    (dist / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    build = FrontendBuild(dist, source)
    assert build.status()["ready"] and "assets/old.js" not in build.files
    original = (source / "src/main.ts").read_bytes()
    (source / "src/main.ts").write_text("new source", encoding="utf-8")
    assert not build.status()["ready"]
    (source / "src/main.ts").write_bytes(original)
    assert build.status()["ready"]
    (dist / "assets/current.js").write_text("partial output", encoding="utf-8")
    assert not build.status()["ready"]
    (dist / "assets/current.js").write_text("current", encoding="utf-8")
    assert build.status()["ready"]
    (source / "src/new.vue").write_text("new component", encoding="utf-8")
    assert not build.status()["ready"]


async def test_chat_shutdown_waits_for_every_worker_cleanup(env, monkeypatch):
    chat, _ = await configured(env)
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def worker():
        nonlocal calls
        calls += 1
        ordinal = calls
        if calls == 3:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            if ordinal == 2:
                cleaning.set()
                await finish.wait()

    monkeypatch.setattr(chat, "loop", worker)
    monkeypatch.setattr(chat, "sticker_loop", worker)
    task = asyncio.create_task(chat.run())
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 3)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), "chat owner exited before its worker released resources"
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
