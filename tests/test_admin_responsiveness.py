"""Management buttons stay responsive without replaying obsolete intentions."""
import asyncio
import hashlib

import pytest
from sqlalchemy import func, select

from purrai.errors import ManagementError
from purrai.models import Action, Group, Inbox, Session, default_settings, now
from purrai.runtime import Runtime
from test_bot_admin import callback, deliver, markup, open_group


def query(token, identifier, actor=1):
    return {
        "id": f"admin-click-{identifier}",
        "chat_instance": "private",
        "from": {"id": actor, "is_bot": False, "first_name": "测试管理员"},
        "data": token,
        "message": {"message_id": 101, "date": int(now()), "chat": {"id": actor, "type": "private"}},
    }


def panel_button(telegram, label):
    return next(item for line in markup(telegram)["reply_markup"]["inline_keyboard"]
                for item in line if label in item["text"])


async def post(runtime, token, identifier, actor=1):
    await runtime.persist([{"update_id": identifier, "callback_query": query(token, identifier, actor)}])
    assert await runtime.process_one("control")
    async with runtime.db.sessions() as session:
        event = await session.get(Inbox, identifier)
        assert event.state == "done", event.error


async def configuration(db):
    async with db.sessions() as session:
        return dict((await session.get(Group, -1001)).settings)


async def test_slow_acknowledgement_does_not_block_panel_or_next_click(env, monkeypatch):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "answer_callback_query":
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    try:
        await asyncio.wait_for(deliver(runtime, {"callback_query": callback(tg, "验证设置")}), 1)
        await asyncio.wait_for(entered.wait(), 1)
        assert "验证设置" in markup(tg)["text"]
        await asyncio.wait_for(deliver(runtime, {"callback_query": callback(tg, "返回群管理")}), 1)
        assert "群聊天设置" in str(markup(tg))
    finally:
        await asyncio.wait_for(runtime.stop(), 1)
    assert not runtime.admin.ui.ack_tasks


async def test_callback_acknowledgements_are_bounded_and_shutdown_cancels_them(env, monkeypatch):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    original = tg.call
    finish = asyncio.Event()
    active = peak = 0

    async def slow(method, **kwargs):
        nonlocal active, peak
        if method == "answer_callback_query":
            active += 1
            peak = max(peak, active)
            try:
                await finish.wait()
            finally:
                active -= 1
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    for identifier in range(80):
        await runtime.admin.ui.acknowledge(str(identifier), "")
    assert len(runtime.admin.ui.ack_tasks) == 64
    assert peak == 8
    await asyncio.wait_for(runtime.stop(), 1)
    assert active == 0 and not runtime.admin.ui.ack_tasks


async def test_slow_panel_edit_then_repeated_old_click_is_explained_and_fresh_click_closes(env, monkeypatch):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_enabled": False}
    await runtime.admin.settings_panel(1, -1001, "verification", 101)
    old = panel_button(tg, "图片验证")["callback_data"]
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "edit_message_text" and not finish.is_set():
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    first = asyncio.create_task(post(runtime, old, 50000))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await configuration(db))["verification_enabled"]
        await runtime.persist([{"update_id": 50001, "callback_query": query(old, 50001)}])
        assert not await runtime.process_one("control")
    finally:
        finish.set()
        await first
    fresh_before_duplicate = panel_button(tg, "图片验证")["callback_data"]
    assert await runtime.process_one("control")
    assert (await configuration(db))["verification_enabled"]
    assert any("本次未重复执行" in data.get("text", "") for method, data in tg.calls
               if method == "answer_callback_query")
    fresh = panel_button(tg, "图片验证")["callback_data"]
    assert fresh != old and fresh == fresh_before_duplicate
    await post(runtime, fresh, 50002)
    assert not (await configuration(db))["verification_enabled"]
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Action).where(Action.kind == "settings")) == 2
    assert not any(data.get("text") == "正在处理" for method, data in tg.calls
                   if method == "answer_callback_query")
    await runtime.stop()


async def test_unchanged_panel_needs_no_edit_and_text_updates_keep_buttons_valid(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    buttons = [("开关", "toggle", {"key": "verification_enabled", "value": False, "section": "verification"})]
    await runtime.admin.panel(1, "计数：1", buttons, -1001, 101)
    first = panel_button(tg, "开关")["callback_data"]
    tg.calls.clear()
    await runtime.admin.panel(1, "计数：1", buttons, -1001, 101)
    assert not tg.calls
    await runtime.admin.panel(1, "计数：2", buttons, -1001, 101)
    assert panel_button(tg, "开关")["callback_data"] == first
    await post(runtime, first, 50500)
    assert not (await configuration(db))["verification_enabled"]
    await runtime.stop()


@pytest.mark.parametrize("legacy", [False, True])
async def test_superseded_panel_cannot_apply_an_unused_old_setting_after_restart(env, legacy):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await runtime.admin.settings_panel(1, -1001, "verification", 101)
    old = panel_button(tg, "申请入群")["callback_data"]
    if legacy:
        record = await runtime.admin.recall("bot_button:" + old[2:])
        record.pop("panel")
        await runtime.admin.remember("bot_button:" + old[2:], record)
    await runtime.admin.settings_panel(1, -1001, "moderation", 101)
    await runtime.admin.ui.close()
    await runtime.chat.close()
    runtime = Runtime(db, tg, service)
    before = await configuration(db)
    await post(runtime, old, 51000)
    assert await configuration(db) == before
    assert "防广告设置" in markup(tg)["text"]
    assert any("面板已更新" in data.get("text", "") for method, data in tg.calls
               if method == "answer_callback_query")
    await runtime.stop()


async def test_failed_panel_update_keeps_previous_panel_until_successful_retry(env, monkeypatch):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await runtime.admin.settings_panel(1, -1001, "verification", 101)
    old = panel_button(tg, "图片验证")["callback_data"]
    previous = await runtime.admin.recall("bot_panel:1:101")
    tg.fail["edit_message_text"] = 1
    await runtime.persist([{"update_id": 52000, "callback_query": query(old, 52000)}])
    assert await runtime.process_one("control")
    assert not (await configuration(db))["verification_enabled"]
    assert await runtime.admin.recall("bot_panel:1:101") == previous
    async with db.transaction() as session:
        (await session.get(Inbox, 52000)).available_at = 0
    assert await runtime.process_one("control")
    assert (await runtime.admin.recall("bot_panel:1:101"))["panel"] != previous["panel"]
    async with db.sessions() as session:
        assert (await session.get(Inbox, 52000)).state == "done"
        assert await session.scalar(select(func.count()).select_from(Action).where(Action.kind == "settings")) == 1
    await runtime.stop()


async def test_panel_from_before_upgrade_refreshes_before_applying_a_setting(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await runtime.admin.settings_panel(1, -1001, "verification", 101)
    old = panel_button(tg, "图片验证")["callback_data"]
    record = await runtime.admin.recall("bot_button:" + old[2:])
    record.pop("panel")
    await runtime.admin.remember("bot_button:" + old[2:], record)
    await runtime.admin.remember("bot_panel:1:101", None)
    await post(runtime, old, 52500)
    assert (await configuration(db))["verification_enabled"]
    assert any("本次未重复执行" in data.get("text", "") for method, data in tg.calls
               if method == "answer_callback_query")
    await post(runtime, panel_button(tg, "图片验证")["callback_data"], 52501)
    assert not (await configuration(db))["verification_enabled"]
    await runtime.stop()


@pytest.mark.parametrize("section", ["verification", "chat"])
async def test_one_role_lookup_per_button_but_next_click_rechecks_current_role(env, section):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    tg.members[(-1001, 2)] = {"status": "administrator"}
    await runtime.admin.settings_panel(2, -1001, section, 101)
    label = "图片验证" if section == "verification" else "群聊天"
    token = panel_button(tg, label)["callback_data"]
    tg.calls.clear()
    await post(runtime, token, 53000, 2)
    lookups = [data for method, data in tg.calls if method == "get_chat_member" and data.get("user_id") == 2]
    assert len(lookups) == 1
    fresh = panel_button(tg, label)["callback_data"]
    before = await configuration(db)
    tg.members[(-1001, 2)] = {"status": "member"}
    await post(runtime, fresh, 53001, 2)
    assert await configuration(db) == before
    assert any("不是此群" in data.get("text", "") for method, data in tg.calls if method == "send_message")
    await runtime.stop()


async def test_role_cache_cannot_be_inherited_by_another_task(env):
    _, _, tg, service = env
    tg.members[(-1001, 2)] = {"status": "administrator"}
    async with service.access.operation():
        await service.access.authorize(2, -1001)
        await service.access.authorize(2, -1001)
        assert len(tg.calls) == 1
        tg.members[(-1001, 2)] = {"status": "member"}
        with pytest.raises(ManagementError, match="不是此群"):
            await asyncio.create_task(service.access.authorize(2, -1001))
    with pytest.raises(ManagementError, match="不是此群"):
        async with service.access.operation():
            await service.access.authorize(2, -1001)


async def test_bulk_action_status_is_one_request_and_preserves_individual_authorization(env, client):
    _, db, tg, _ = env
    tg.members[(-1001, 2)] = {"status": "administrator"}
    async with db.transaction() as session:
        login = await session.get(Session, hashlib.sha256(b"test-session").hexdigest())
        login.user_id = 2
        session.add(Group(id=-1002, title="无权管理的群", approved=True, settings=default_settings()))
        await session.flush()
        for identifier, group in (("a1", -1001), ("a2", -1001), ("denied", -1002), ("root", None)):
            session.add(Action(id=identifier, kind="delete", group_id=group, dedupe_key=identifier, state="pending"))
    params = [("identifiers", value) for value in ("a1", "a2", "denied", "missing", "root", "a1")]
    response = await client.get("/api/actions", params=params)
    assert response.status_code == 200
    data = response.json()
    assert {row["id"] for row in data["items"]} == {"a1", "a2"}
    assert {key: value["status"] for key, value in data["errors"].items()} == {"denied": 403, "missing": 404, "root": 403}
    assert len([1 for method, _ in tg.calls if method == "get_chat_member"]) == 2
    tg.members[(-1001, 2)] = {"status": "member"}
    denied = await client.get("/api/actions", params={"identifiers": "a1"})
    assert denied.json()["errors"]["a1"]["status"] == 403
    assert (await client.get("/api/actions")).status_code == 422
    too_many = [("identifiers", str(i)) for i in range(51)]
    assert (await client.get("/api/actions", params=too_many)).status_code == 422
