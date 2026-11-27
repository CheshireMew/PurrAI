"""Full model diagnostics through actual HTTP serialization and admin reads."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time

import pytest
from sqlalchemy import select

from conftest import message
from purrai.chat import GroupChat
from purrai.chat_model import ChatModel, ChatModelError
from purrai.model_diagnostics import ModelDiagnostics
from purrai.models import ChatTurn, Session


@pytest.fixture
def provider():
    state = {"requests": [], "status": 200, "started": threading.Event(), "release": threading.Event()}
    reply = json.dumps({"should_reply": True, "is_advertisement": False, "reply": "这是本地端点的完整回复。"}, ensure_ascii=False)
    state["body"] = json.dumps({"choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                                "usage": {"total_tokens": 42}}, ensure_ascii=False, indent=2).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append(body)
            state["started"].set()
            if state.get("block"):
                state["release"].wait(3)
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            try:
                self.wfile.write(state["body"])
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    state["release"].set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def configure(settings, provider, *, enabled=True):
    settings.chat_api_base = provider["url"]
    settings.chat_api_key = "test-model-credential-must-be-redacted"
    settings.chat_model = "local-contract-model"
    settings.model_diagnostics_enabled = enabled


async def test_group_chat_records_actual_wire_and_admin_reads_same_turn(env, client, provider):
    from test_chat import due

    settings, db, tg, _ = env
    configure(settings, provider)
    tg.username = "purrai_bot"
    assert (await client.patch("/api/groups/-1001/settings", json={"settings": {"chat_enabled": True}})).status_code == 200
    chat = GroupChat(db, tg, settings)
    try:
        await chat.observe(message(701, "@purrai_bot 给我一句完整的回复"))
        await due(chat)
        assert await chat.run_one()
    finally:
        await chat.model.close()
    assert any(data.get("text") == "这是本地端点的完整回复。" for method, data in tg.calls if method == "send_message")
    page = await client.get("/api/system/model-diagnostics")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert page.json()["total"] == 1
    item = page.json()["items"][0]
    async with db.sessions() as session:
        turn = await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 701))
        assert item["context"]["turn_id"] == turn.id
    response = await client.get(f"/api/system/model-diagnostics/{item['id']}")
    record = response.json()
    assert record["state"] == "succeeded" and record["complete"]
    assert record["request"]["body_utf8"].encode() == provider["requests"][0]
    assert record["response"]["body_utf8"].encode() == provider["body"]
    assert settings.chat_api_key not in json.dumps(record)
    assert "authorization" not in record["request"]
    async with db.transaction() as session:
        (await session.scalar(select(Session))).user_id = 77
    tg.members[(-1001, 77)] = {"status": "administrator"}
    assert (await client.get("/api/system/model-diagnostics")).status_code == 403
    assert (await client.get(f"/api/system/model-diagnostics/{item['id']}")).status_code == 403
    client.cookies.clear()
    assert (await client.get("/api/system/model-diagnostics")).status_code == 401


async def test_diagnostics_disabled_does_not_create_files(env, provider):
    settings, _, _, _ = env
    configure(settings, provider, enabled=False)
    model = ChatModel(settings)
    store = ModelDiagnostics(settings)
    try:
        await model.completion("系统内容", "普通输入")
    finally:
        await model.close()
    assert not store.root.exists()


@pytest.mark.parametrize("status", [200, 503])
async def test_invalid_and_vendor_error_bodies_are_recorded_and_credentials_redacted(env, provider, status):
    settings, _, _, _ = env
    configure(settings, provider)
    provider["status"] = status
    provider["body"] = json.dumps({"error": {"message": "完整厂商错误\n第二行", "api_key": settings.chat_api_key}}).encode()
    model = ChatModel(settings)
    try:
        with pytest.raises(ChatModelError) as error:
            await model.completion("系统", "输入", api_extra={"nested": {"access_token": "private-extra-token"}})
    finally:
        await model.close()
    store = ModelDiagnostics(settings)
    item = store.page()["items"][0]
    record = store.read(item["id"])
    assert error.value.request_id == item["id"]
    assert record["state"] == "failed" and record["response"]["status_code"] == status
    assert json.loads(record["response"]["body_utf8"])["error"]["message"] == "完整厂商错误\n第二行"
    assert "private-extra-token" not in json.dumps(record)
    assert settings.chat_api_key not in json.dumps(record)


async def test_diagnostics_retention_and_capacity_remove_old_owned_records(env, provider):
    settings, _, _, _ = env
    configure(settings, provider)
    settings.model_diagnostics_max_mb = 1
    model = ChatModel(settings)
    store = ModelDiagnostics(settings)
    try:
        await model.completion("系统", "甲" * 180000)
        first = store.page()["items"][0]["id"]
        await model.completion("系统", "乙" * 180000)
    finally:
        await model.close()
    assert first not in {item["id"] for item in store.page()["items"]}
    paths = list(store.root.glob("*.json"))
    assert len(paths) == 1
    assert sum(path.stat().st_size for path in paths) <= 1048576
    past = time.time() - settings.model_diagnostics_retention_hours * 3600 - 1
    foreign = store.root / ("f" * 32 + ".json")
    foreign.write_text('{"user_file":"preserve"}', encoding="utf-8")
    os.utime(foreign, (past, past))
    os.utime(paths[0], (past, past))
    store.prune()
    assert store.page()["total"] == 0
    assert foreign.read_text(encoding="utf-8") == '{"user_file":"preserve"}'


async def test_cancelled_request_preserves_request_and_cancelled_outcome(env, provider):
    settings, _, _, _ = env
    configure(settings, provider)
    provider["block"] = True
    model = ChatModel(settings)
    task = asyncio.create_task(model.completion("系统", "取消输入"))
    try:
        # Cancellation is checked after the server receives the request, not
        # against client initialization or filesystem scheduling under load.
        async with asyncio.timeout(5):
            while not provider["started"].is_set():
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        provider["release"].set()
        await model.close()
    store = ModelDiagnostics(settings)
    record = store.read(store.page()["items"][0]["id"])
    assert record["state"] == "cancelled" and record["request"]["body_utf8"].encode() == provider["requests"][0]
    assert record["error"]["type"] == "CancelledError"
