import pytest
from sqlalchemy import select

from conftest import drain
from purrai.bot_admin import CHAT_FIELDS, button
from purrai.models import Action, Group
from purrai.runtime import Runtime
from test_bot_admin import callback, click, deliver, markup, open_group, private, texts


async def expire(runtime, query, *, legacy=False):
    key = "bot_button:" + query["data"][2:]
    record = await runtime.admin.recall(key)
    record["expires"] = 0
    if legacy:
        record.pop("resume", None)
    await runtime.admin.remember(key, record)


async def test_legacy_expired_chat_refresh_resumes_after_restart_and_shows_two_columns(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, "群聊天设置")
    query = callback(tg, "刷新聊天状态")
    await expire(runtime, query, legacy=True)
    runtime = Runtime(db, tg, service)
    await deliver(runtime, {"callback_query": query})
    panel = markup(tg)
    assert "群聊天设置" in panel["text"]
    assert panel["message_id"] == query["message"]["message_id"]
    rows = panel["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [2] * 9 + [1]
    assert any("自动识别口令" in item["text"] for row in rows for item in row)
    assert any("口令识别次数" in item["text"] for row in rows for item in row)
    assert rows[-1][0]["text"] == "返回群管理"
    assert all("addstickers" not in item["text"] for row in rows for item in row)
    refreshed = callback(tg, "刷新聊天状态")
    assert (await runtime.admin.recall("bot_button:" + refreshed["data"][2:]))["expires"] is None
    await click(runtime, tg, "聊天贴纸包")
    assert CHAT_FIELDS["chat_sticker_packs"] in texts(tg)[-1]


@pytest.mark.parametrize("legacy", [False, True])
async def test_expired_chat_switch_refreshes_current_values_without_changing_settings(env, legacy):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, "群聊天设置")
    query = callback(tg, "主动参与讨论")
    await expire(runtime, query, legacy=legacy)
    async with db.sessions() as session:
        before = dict((await session.get(Group, -1001)).settings)
    runtime = Runtime(db, tg, service)
    await deliver(runtime, {"callback_query": query})
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings == before
    assert "群聊天设置" in markup(tg)["text"]
    await click(runtime, tg, "主动参与讨论")
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["chat_proactive"] is not before["chat_proactive"]


async def test_expired_confirmation_is_renewed_before_execution(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, "成员放行")
    await click(runtime, tg, "临时禁言")
    await deliver(runtime, {"message": private("42 核实后处理")})
    query = callback(tg, "确认执行")
    await expire(runtime, query, legacy=True)
    runtime = Runtime(db, tg, service)
    await deliver(runtime, {"callback_query": query})
    assert "请确认" in markup(tg)["text"]
    async with db.sessions() as session:
        assert not (await session.scalars(select(Action).where(Action.kind == "manual_mute"))).all()
    await click(runtime, tg, "确认执行")
    await drain(service)
    assert tg.members[(-1001, 42)]["status"] == "restricted"
    async with db.sessions() as session:
        assert len((await session.scalars(select(Action).where(Action.kind == "manual_mute"))).all()) == 1


async def test_unknown_old_button_recovers_selected_group_without_admin_command(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    query = callback(tg, "验证设置")
    query["data"] = "a:old-unknown-token"
    runtime = Runtime(db, tg, service)
    await deliver(runtime, {"callback_query": query})
    assert "群聊天设置" in str(markup(tg))
    assert "重新打开" not in texts(tg)[-1]


async def test_removed_old_menu_action_returns_to_current_group_menu(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    query = callback(tg, "验证设置")
    key = "bot_button:" + query["data"][2:]
    record = await runtime.admin.recall(key)
    record["action"] = "retired-settings-screen"
    await runtime.admin.remember(key, record)
    await deliver(runtime, {"callback_query": query})
    assert "群聊天设置" in str(markup(tg))


async def test_expired_navigation_still_rechecks_revoked_administrator(env):
    _, db, tg, service = env
    tg.members[(-1001, 2)] = {"status": "administrator"}
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg, actor=2)
    await click(runtime, tg, "群聊天设置", actor=2)
    query = callback(tg, "刷新聊天状态", actor=2)
    await expire(runtime, query, legacy=True)
    tg.members[(-1001, 2)] = {"status": "member"}
    tg.calls.clear()
    runtime = Runtime(db, tg, service)
    await deliver(runtime, {"callback_query": query})
    assert "不是" in texts(tg)[-1]
    assert not any(method == "edit_message_text" for method, _ in tg.calls)


async def test_another_administrator_cannot_resume_someone_elses_button(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    query = callback(tg, "验证设置", actor=2)
    tg.members[(-1001, 2)] = {"status": "administrator"}
    await expire(runtime, query, legacy=True)
    tg.calls.clear()
    await deliver(runtime, {"callback_query": query})
    assert "其他管理员" in texts(tg)[-1]
    assert not any(method == "edit_message_text" for method, _ in tg.calls)


async def test_long_labels_stay_full_width_and_every_button_remains_available(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    labels = ["验证设置", "防广告设置", "这是需要完整显示的很长的操作说明按钮", "上一页", "下一页", "返回群管理"]
    await runtime.admin.panel(1, "布局", [button(label, "groups") for label in labels])
    rows = markup(tg)["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 1, 2, 1]
    assert [item["text"] for row in rows for item in row] == labels
