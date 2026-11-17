import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.methods import GetMe
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from conftest import message
from purrai.app import create_app
from purrai.models import Action, Meta, now
from purrai.runtime import Runtime
from purrai.telegram import TelegramGateway
from purrai.telegram_health import TelegramHealth, TracedSession, request_identity, request_trace, request_purpose


async def test_real_http_trace_reports_response_wait_without_leaking_request(caplog):
    started, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        started.set()
        await release.wait()
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/private-token", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    adapter = TracedSession()
    trace = {"phase": "prepare"}
    token = request_trace.set(trace)
    try:
        sessions = await asyncio.gather(adapter.create_session(), adapter.create_session())
        assert sessions[0] is sessions[1]
        assert sessions[0].trace_configs.count(adapter.trace) == 1
        with pytest.raises(TimeoutError):
            await sessions[0].get(f"http://127.0.0.1:{port}/private-token", timeout=0.1)
        assert started.is_set()
        assert trace["phase"] == "response_headers"
        release.set()
        async with sessions[0].get(f"http://127.0.0.1:{port}/private-token") as response:
            assert await response.text() == "ok"
        assert trace["phase"] == "decode"
        assert "private-token" not in caplog.text
    finally:
        request_trace.reset(token)
        release.set()
        await adapter.close()
        await runner.cleanup()


@pytest.mark.parametrize("exception,outcome", [
    (TelegramNetworkError(method=GetMe(), message="https://api.example/bot-secret private body"), "network_error"),
    (TelegramUnauthorizedError(method=GetMe(), message="private credential"), "unauthorized"),
    (TelegramRetryAfter(method=GetMe(), message="private rate limit", retry_after=20), "rate_limited"),
    (TelegramBadRequest(method=GetMe(), message="private bad request"), "rejected"),
])
async def test_gateway_classifies_faults_preserves_exception_and_redacts(caplog, exception, outcome):
    gateway = TelegramGateway("")

    async def fail(**kwargs):
        raise exception

    gateway.bot = SimpleNamespace(get_me=fail)
    identity = request_identity.set("action=123 kind=owner_alert group=None")
    try:
        with caplog.at_level(logging.INFO), pytest.raises(type(exception)) as caught:
            await gateway.call("get_me", request_timeout=7)
        assert caught.value is exception
        assert request_trace.get() is None
        row = gateway.health.status()["methods"][0]
        assert row["outcome"] == outcome and row["timeout"] == 7
        assert gateway.health.status()["degraded"] == (outcome != "rejected")
        assert "private" not in caplog.text and "bot-secret" not in caplog.text
        if outcome != "rejected":
            assert "action=123" in caplog.text and "method=get_me" in caplog.text
    finally:
        request_identity.reset(identity)


async def test_partial_fault_survives_receipt_recovery_and_restart_then_clears_on_same_method(env, caplog):
    settings, db, tg, service = env
    tg.health = TelegramHealth()
    runtime = Runtime(db, tg, service)
    tg.health.record("send_message", "network_error", 10, {"phase": "response_headers"}, 10, "TimeoutError")
    tg.health.record("get_updates", "ok", 0.2, {"phase": "decode"}, 35)
    with caplog.at_level(logging.INFO):
        await runtime.alerts.cycle()
    assert "发送文字消息" in caplog.text
    tg.health = TelegramHealth()
    restarted = Runtime(db, tg, service)
    await restarted.restore_health()
    assert tg.health.status()["degraded"]
    assert tg.health.status()["problems"][0]["method"] == "send_message"
    await restarted.alerts.cycle()
    async with db.sessions() as session:
        assert len((await session.scalars(select(Action).where(Action.kind == "owner_alert"))).all()) == 0
    tg.health.record("send_message", "ok", 0.1, {"phase": "decode"}, 10)
    await restarted.alerts.cycle()
    async with db.sessions() as session:
        marker = json.loads((await session.get(Meta, "runtime-alert:Telegram 接口")).value)
        assert not marker["active"]
        assert len((await session.scalars(select(Action).where(Action.kind == "owner_alert"))).all()) == 0


async def test_only_fresh_repeated_api_failures_notify_and_poll_fault_does_not_duplicate(env, monkeypatch):
    from purrai.runtime_alerts import RuntimeAlerts
    from purrai import runtime_alerts

    settings, db, _, _ = env
    instant = [now()]
    monkeypatch.setattr(runtime_alerts, "now", lambda: instant[0])
    problem = {"method": "send_message", "label": "发送文字消息", "seconds": 10, "consecutive_failures": 1,
               "first_failure": instant[0], "last_failure": instant[0]}
    status = {"poll_error": "", "telegram_health": {"problems": [problem]}}
    alerts = RuntimeAlerts(db, settings, lambda: status)
    await alerts.cycle()
    instant[0] += settings.alert_backlog_seconds + 1
    await alerts.cycle()  # An old single failure alone must stay quiet.
    async with db.sessions() as session:
        assert not (await session.scalars(select(Action))).all()
    problem.update(last_failure=instant[0], consecutive_failures=2)
    status["poll_error"] = "TimeoutError"
    await alerts.cycle()  # Let the connection alert explain a common outage.
    async with db.sessions() as session:
        assert not (await session.scalars(select(Action))).all()
    status["poll_error"] = ""
    await alerts.cycle()
    await RuntimeAlerts(db, settings, lambda: status).cycle()
    async with db.sessions() as session:
        notices = (await session.scalars(select(Action))).all()
        assert len(notices) == 1 and notices[0].payload["alert_key"] == "Telegram 接口"
    status["telegram_health"]["problems"] = []
    await alerts.cycle()
    async with db.sessions() as session:
        assert len((await session.scalars(select(Action))).all()) == 1


async def test_readiness_and_system_show_partial_telegram_failure(env):
    settings, db, tg, _ = env
    settings.bot_token = "999:TEST_TOKEN_FOR_OFFLINE_TESTS_ONLY"
    tg.health = TelegramHealth()
    app = create_app(settings, db, tg, start_runtime=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health/ready")).status_code == 200
        tg.health.record("send_photo", "network_error", 30, {"phase": "response_headers"}, 30, "TimeoutError")
        reply = await client.get("/health/ready")
        assert reply.status_code == 503 and reply.json()["status"] == "telegram_degraded"
        assert (await client.get("/health/live")).status_code == 200
        tg.health.record("send_photo", "ok", 0.3, {"phase": "decode"}, 30)
        assert (await client.get("/health/ready")).status_code == 200


async def test_auxiliary_fault_warns_without_hiding_core_or_authentication_failures(env):
    settings, db, tg, _ = env
    settings.bot_token = "999:TEST_TOKEN_FOR_OFFLINE_TESTS_ONLY"
    tg.health = TelegramHealth()
    app = create_app(settings, db, tg, start_runtime=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = request_purpose.set("source_check")
        try:
            tg.health.record("get_chat_member", "network_error", 10, {"phase": "response_headers"}, 10)
        finally:
            request_purpose.reset(token)
        response = await client.get("/health/ready")
        assert response.status_code == 200 and response.json()["auxiliary_warning"]
        tg.health.record("get_chat_member", "network_error", 10, {"phase": "response_headers"}, 10)
        token = request_purpose.set("source_check")
        try:
            tg.health.record("get_chat_member", "ok", 0.1, {"phase": "decode"}, 10)
        finally:
            request_purpose.reset(token)
        assert (await client.get("/health/ready")).status_code == 503
        assert tg.health.status()["problems"][0]["purpose"] == "operations"
        tg.health.record("get_chat_member", "ok", 0.1, {"phase": "decode"}, 10)
        token = request_purpose.set("source_check")
        try:
            tg.health.record("get_chat_member", "unauthorized", 0.1, {"phase": "decode"}, 10)
            saved = tg.health.status()
            tg.health = TelegramHealth()
            tg.health.restore(saved)
            tg.health.record("get_chat_member", "network_error", 10, {"phase": "response_headers"}, 10)
        finally:
            request_purpose.reset(token)
        assert (await client.get("/health/ready")).status_code == 503


def test_restored_legacy_source_fault_keeps_evidence_at_warning_level():
    health = TelegramHealth()
    health.record("edit_message_reply_markup", "network_error", 3, {"phase": "response_headers"}, 3)
    saved = health.status()
    for row in saved["methods"]:
        row.pop("purpose")
    health = TelegramHealth()
    health.restore(saved)
    assert not health.status()["degraded"]
    assert health.status()["warnings"][0]["failures"] == 1
    token = request_purpose.set("source_check")
    try:
        health.record("edit_message_reply_markup", "rejected", 0.2, {"phase": "decode"}, 10)
    finally:
        request_purpose.reset(token)
    assert not health.status()["warnings"]


async def test_source_warning_persists_across_other_success_and_notifies_once_after_grace(env, monkeypatch):
    from purrai import runtime_alerts
    settings, db, tg, service = env
    runtime = Runtime(db, tg, service)
    tg.health = TelegramHealth()
    instant = [now()]
    monkeypatch.setattr(runtime_alerts, "now", lambda: instant[0])
    item = {"failures": 1, "at": instant[0], "first_failure": instant[0], "next": instant[0] + 30}
    runtime.chat.source_checks.state = {"items": {"1": item}}
    await runtime.alerts.cycle()
    instant[0] += settings.alert_backlog_seconds + 1
    await runtime.alerts.cycle()  # No escalation from an old single failure.
    async with db.sessions() as session:
        assert not (await session.scalars(select(Action))).all()
    item.update(failures=2, at=instant[0], next=instant[0] + 60)
    token = request_purpose.set("source_check")
    try:
        tg.health.record("edit_message_reply_markup", "ok", 0.1, {"phase": "decode"}, 10)
    finally:
        request_purpose.reset(token)
    await runtime.alerts.cycle()
    await runtime.alerts.cycle()
    async with db.sessions() as session:
        marker = json.loads((await session.get(Meta, "runtime-alert:原消息核对")).value)
        assert marker["active"] and marker["severity"] == "warning" and marker["episode"] == 1
        notices = (await session.scalars(select(Action))).all()
        assert len(notices) == 1 and notices[0].payload["alert_key"] == "原消息核对"
    runtime.chat.source_checks.state = {"items": {}}
    await runtime.alerts.cycle()
    async with db.sessions() as session:
        marker = json.loads((await session.get(Meta, "runtime-alert:原消息核对")).value)
        assert not marker["active"] and marker["episode"] == 1


async def test_missing_poll_progress_is_not_reported_as_recovered(env):
    settings, db, tg, service = env
    runtime = Runtime(db, tg, service)
    runtime.started_at = now() - 100
    assert runtime.status()["poll_stalled"]
    await runtime.alerts.cycle()
    async with db.sessions() as session:
        marker = json.loads((await session.get(Meta, "runtime-alert:Telegram 收件")).value)
        assert marker["active"] and "90秒" in marker["message"]
        assert "自动重连" not in marker["message"]
    runtime.receiver.last_poll = now()
    assert not runtime.status()["poll_stalled"]


def test_repeated_fault_logs_are_bounded_and_recovery_is_logged(caplog):
    health = TelegramHealth()
    with caplog.at_level(logging.INFO):
        for _ in range(10):
            health.record("get_chat", "network_error", 10, {"phase": "connect"}, 10, "TimeoutError")
        health.record("get_chat", "rejected", 0.1, {"phase": "decode"}, 10)
    assert caplog.text.count("Telegram接口异常") == 1
    assert "failed_calls=10" in caplog.text
    assert not health.status()["degraded"]


async def test_alert_monitor_runs_while_backup_is_blocked(env, monkeypatch):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    backup_started, alert_checked = asyncio.Event(), asyncio.Event()

    async def backup():
        backup_started.set()
        await asyncio.Future()

    async def alert():
        alert_checked.set()

    monkeypatch.setattr(runtime.management.backups, "cycle", backup)
    monkeypatch.setattr(runtime.alerts, "cycle", alert)
    workers = []
    monkeypatch.setattr(runtime.workers, "start", workers.extend)
    runtime.start()
    selected = dict(workers)
    maintenance = asyncio.create_task(selected["operations-maintenance"]())
    monitor = None
    try:
        await asyncio.wait_for(backup_started.wait(), 1)
        monitor = asyncio.create_task(selected["runtime-alerts"]())
        await asyncio.wait_for(alert_checked.wait(), 1)
        assert not maintenance.done()
    finally:
        tasks = [maintenance, *([monitor] if monitor else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_inbox_network_diagnostics_have_event_identity_and_reset_context(env, monkeypatch):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    seen = []

    async def dispatch(payload):
        seen.append(request_identity.get())

    monkeypatch.setattr(runtime.inbox, "dispatch", dispatch)
    await runtime.persist([{"update_id": 456, "message": message(78, "测试接收")}])
    assert await runtime.process_one()
    assert "update=456" in seen[0] and "-1001" in seen[0]
    assert request_identity.get() == ""
