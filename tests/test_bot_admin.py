import itertools

import pytest
from aiogram.types import Update
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from conftest import drain, join, message
from purrai import captcha
from purrai.app import create_app
from purrai.management import ManagementError
from purrai.models import Action, Group, Inbox, Rule, Verification, Violation, Whitelist, default_settings
from purrai.runtime import Runtime
from purrai.telegram import TelegramGateway


sequence = itertools.count(1000)


def private(text, actor=1):
    return message(next(sequence), text, actor, chat={"id": actor, "type": "private"})


async def deliver(runtime, payload):
    identifier = next(sequence)
    event = (
        payload.get("message")
        or payload.get("edited_message")
        or payload.get("callback_query")
        or payload.get("chat_member")
    )
    if event:
        event["from"].update(is_bot=False, first_name="测试管理员")
        if "message" in event:
            event["message"]["from"].update(is_bot=True, first_name="PurrAI")
    Update.model_validate({"update_id": identifier, **payload})
    await runtime.persist([{"update_id": identifier, **payload}])
    assert await runtime.process_one()
    async with runtime.db.sessions() as session:
        row = await session.get(Inbox, identifier)
        assert row.state == "done", row.error


def markup(telegram):
    return next(values for method, values in reversed(telegram.calls) if "reply_markup" in values)


def callback(telegram, label, actor=1):
    panel = markup(telegram)
    item = next(b for row in panel["reply_markup"]["inline_keyboard"] for b in row if label in b["text"])
    return {
        "id": str(next(sequence)),
        "from": {"id": actor},
        "chat_instance": "private",
        "data": item["callback_data"],
        "message": private("面板", actor),
    }


async def click(runtime, telegram, label, actor=1):
    query = callback(telegram, label, actor)
    await deliver(runtime, {"callback_query": query})
    return query


def texts(telegram):
    return [data["text"] for method, data in telegram.calls if method == "send_message"]


async def open_group(runtime, telegram, actor=1):
    await deliver(runtime, {"message": private("/admin", actor)})
    await click(runtime, telegram, "测试群", actor)


async def test_private_bot_admin_needs_no_web_login_and_denies_other_users(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    assert "验证设置" in str(markup(telegram))
    assert await runtime.admin.recall("bot_selected:1") == -1001
    await deliver(runtime, {"message": private("/admin", 7)})
    assert "不是机器人已加入群的管理员" in texts(telegram)[-1]
    await deliver(runtime, {"message": private("/start", 7)})
    assert "完整答案" in texts(telegram)[-1]


async def test_bot_settings_validate_persist_across_runtime_restart_and_cancel_navigation(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    await click(runtime, telegram, "验证设置")
    toggle = await click(runtime, telegram, "图片验证")
    await deliver(runtime, {"callback_query": toggle})
    async with database.sessions() as session:
        assert (await session.get(Group, -1001)).settings["verification_enabled"] is False
    await click(runtime, telegram, "验证时限")
    runtime = Runtime(database, telegram, service)
    await deliver(runtime, {"message": private("12")})
    assert "30" in texts(telegram)[-1]
    assert await runtime.admin.recall("bot_prompt:1")
    await deliver(runtime, {"message": private("240")})
    async with database.sessions() as session:
        group = await session.get(Group, -1001)
        assert group.settings["verification_timeout"] == 240
        assert group.settings["verification_enabled"] is False
    await click(runtime, telegram, "验证时限")
    await click(runtime, telegram, "返回群管理")
    await deliver(runtime, {"message": private("300")})
    async with database.sessions() as session:
        assert (await session.get(Group, -1001)).settings["verification_timeout"] == 240
    assert not await runtime.admin.recall("bot_prompt:1")


async def test_bot_rule_entry_preview_domain_validation_and_enable_switch(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    await click(runtime, telegram, "广告规则与测试")
    await click(runtime, telegram, "添加关键词")
    incoming = private("广告")
    await deliver(runtime, {"message": incoming})
    await deliver(runtime, {"message": incoming})
    async with database.sessions() as session:
        rows = (await session.scalars(select(Rule))).all()
        assert len(rows) == 1 and rows[0].value == "广告"
    await click(runtime, telegram, "测试当前规则")
    await deliver(runtime, {"message": private("来看看广告")})
    assert "关键词" in texts(telegram)[-1]
    async with database.sessions() as session:
        assert not (await session.scalars(select(Violation))).all()
    await runtime.admin.list_panel(1, -1001, "rule")
    await click(runtime, telegram, "关键词 · 广告")
    await click(runtime, telegram, "改为完整匹配")
    await click(runtime, telegram, "停用")
    async with database.sessions() as session:
        rule = await session.scalar(select(Rule))
        assert rule.mode == "exact" and not rule.enabled
    await click(runtime, telegram, "返回列表")
    await click(runtime, telegram, "添加禁止域名")
    await deliver(runtime, {"message": private("https://bad.example/a")})
    assert "只填写主机名" in texts(telegram)[-1]
    await deliver(runtime, {"message": private("bad.example")})
    async with database.sessions() as session:
        assert len((await session.scalars(select(Rule))).all()) == 2


async def test_bot_authorization_checks_scoped_grants_and_current_telegram_role(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    telegram.members[(-1001, 2)] = {"status": "administrator"}
    async with database.transaction() as session:
        session.add(Group(approved=True, id=-1002, title="另一群", type="supergroup", settings=default_settings()))
    await open_group(runtime, telegram, 2)
    assert "管理员授权" not in str(markup(telegram))
    with pytest.raises(ManagementError, match="不是此群"):
        await runtime.management.authorize(2, -1002)
    await click(runtime, telegram, "验证设置", 2)
    await click(runtime, telegram, "图片验证", 2)
    async with database.sessions() as session:
        assert not (await session.get(Group, -1001)).settings["verification_enabled"]
    telegram.members[(-1001, 2)] = {"status": "member"}
    await click(runtime, telegram, "图片验证", 2)
    assert "不是此群的 Telegram 管理员" in texts(telegram)[-1]
    async with database.sessions() as session:
        assert not (await session.get(Group, -1001)).settings["verification_enabled"]


async def test_bot_buttons_are_actor_bound_expire_and_survive_restart(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    query = callback(telegram, "暂停群保护", actor=2)
    await deliver(runtime, {"callback_query": query})
    async with database.sessions() as session:
        assert (await session.get(Group, -1001)).enabled
    query["from"]["id"] = 1
    query["message"]["chat"]["id"] = 1
    runtime = Runtime(database, telegram, service)
    await deliver(runtime, {"callback_query": query})
    async with database.sessions() as session:
        assert not (await session.get(Group, -1001)).enabled
    query = callback(telegram, "检查并启用")
    key = "bot_button:" + query["data"][2:]
    row = await runtime.admin.recall(key)
    row["expires"] = 0
    await runtime.admin.remember(key, row)
    await deliver(runtime, {"callback_query": query})
    async with database.sessions() as session:
        assert not (await session.get(Group, -1001)).enabled
    assert "检查并启用" in str(markup(telegram))
    assert any("已接续" in args.get("text", "") for method, args in telegram.calls
               if method == "answer_callback_query")


async def test_bot_verification_confirm_double_click_only_releases_once(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    await service.member_event(join())
    await drain(service)
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    await click(runtime, telegram, "待验证成员")
    await click(runtime, telegram, "(42)")
    await click(runtime, telegram, "放行")
    async with database.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "pending"
    query = await click(runtime, telegram, "确认执行")
    assert "已提交" in texts(telegram)[-1]
    assert not any("执行结果" in text for text in texts(telegram))
    await drain(service)
    assert "已完成" in texts(telegram)[-1]
    await deliver(runtime, {"callback_query": query})
    await drain(service)
    async with database.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "passed"
        releases = (await session.scalars(select(Action).where(Action.kind == "release"))).all()
        assert len(releases) == 1 and releases[0].actor_id == 1
    assert len([m for m, p in telegram.calls if m == "restrict_chat_member"]) == 2


async def test_bot_group_reply_commands_and_completed_command_replay(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    reply = message(55, "广告", 42)
    reply["from"].update(is_bot=False, first_name="成员")
    incoming = message(56, "/mute 请冷静", 1, reply_to_message=reply)
    await deliver(runtime, {"message": incoming})
    await drain(service)
    assert telegram.members[(-1001, 42)]["status"] == "restricted"
    await deliver(runtime, {"message": incoming})
    await drain(service)
    assert len([m for m, p in telegram.calls if m == "restrict_chat_member"]) == 1
    await deliver(runtime, {"message": message(57, "/unmute\n42 核实后解除", 1)})
    await drain(service)
    assert telegram.members[(-1001, 42)]["can_send_messages"]
    assert any("执行结果" in text and "已完成" in text for text in texts(telegram))


async def test_bot_unmute_active_verification_finishes_it_and_old_buttons_do_not_release_again(
    env, monkeypatch
):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    await service.member_event(join())
    await drain(service)
    runtime = Runtime(database, telegram, service)
    incoming = message(66, "/unmute 42 核实身份", 1)
    await deliver(runtime, {"message": incoming})
    await drain(service)
    await deliver(runtime, {"message": incoming})
    await drain(service)
    await service.scan_timeouts()
    async with database.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "passed"
    assert not any(method == "ban_chat_member" for method, _ in telegram.calls)
    assert len([m for m, p in telegram.calls if m == "restrict_chat_member"]) == 2


async def test_bot_pending_members_cannot_bypass_message_cleanup_using_commands(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    await service.member_event(join())
    await drain(service)
    runtime = Runtime(database, telegram, service)
    await deliver(runtime, {"message": message(77, "/help https://spam.example", 42)})
    await drain(service)
    assert any(method == "delete_message" and p["message_id"] == 77 for method, p in telegram.calls)


async def test_bot_execution_notice_waits_for_retry_and_reports_actual_failure(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    telegram.fail["restrict_chat_member"] = 1
    await deliver(runtime, {"message": message(90, "/mute 42 核实", 1)})
    await drain(service)
    assert not any("执行结果" in text for text in texts(telegram))
    async with database.transaction() as session:
        for row in (await session.scalars(select(Action).where(Action.state == "pending"))).all():
            row.available_at = 0
    await drain(service)
    assert any("执行结果" in text and "已完成" in text for text in texts(telegram))
    telegram.members[(-1001, 43)] = {"status": "restricted", "is_member": True, "until_date": 0}
    await deliver(runtime, {"message": message(91, "/mute 43 请核实", 1)})
    await drain(service)
    assert "失败" in texts(telegram)[-1] and "管理员已有禁言" in texts(telegram)[-1]


async def test_bot_whitelist_and_automatic_permission_instructions_without_web(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    await click(runtime, telegram, "白名单")
    await click(runtime, telegram, "添加白名单成员")
    await deliver(runtime, {"message": private("42 测试成员")})
    await click(runtime, telegram, "42 · 测试成员")
    await click(runtime, telegram, "切换免验证")
    async with database.sessions() as session:
        row = await session.get(Whitelist, (-1001, 42))
        assert not row.verification and row.moderation
    await click(runtime, telegram, "移出白名单")
    await click(runtime, telegram, "确认执行")
    async with database.sessions() as session:
        assert await session.get(Whitelist, (-1001, 42)) is None
    await deliver(runtime, {"message": private("/admin")})
    await click(runtime, telegram, "权限与账号说明")
    instructions = markup(telegram)["text"]
    assert "获准群的群主和管理员" in instructions
    assert "本群管理权限" in instructions or "管理本群" in instructions
    telegram.members[(-1001, 2)] = {"status": "administrator"}
    assert [row["id"] for row in await runtime.management.groups(2)] == [-1001]
    telegram.members[(-1001, 2)] = {"status": "member"}
    with pytest.raises(ManagementError, match="不是机器人已加入群的管理员"):
        await runtime.management.principal(2)


async def test_bot_batch_release_records_violation_correction_and_shared_http_state(env, client, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    for member in (42, 43):
        await service.member_event(join(member))
    await drain(service)
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    await click(runtime, telegram, "待验证成员")
    await click(runtime, telegram, "批量放行")
    await click(runtime, telegram, "确认执行")
    await drain(service)
    async with database.sessions() as session:
        assert {v.state for v in (await session.scalars(select(Verification))).all()} == {"passed"}
    response = await client.get("/api/groups/-1001/verifications")
    assert response.status_code == 200 and all(row["state"] == "passed" for row in response.json()["items"])
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="广告"))
    # Manually released members are now whitelisted; use a different sender for
    # the independent legacy-warning correction workflow.
    await service.message(message(121, "广告", 44))
    await drain(service)
    # Old warnings remain correctable; current message filtering does not create new warnings.
    async with database.transaction() as session:
        (await session.scalar(select(Violation))).warning_active = True
    await runtime.admin.list_panel(1, -1001, "violation")
    await click(runtime, telegram, "用户 44")
    await click(runtime, telegram, "撤销警告")
    await click(runtime, telegram, "确认执行")
    await drain(service)
    async with database.sessions() as session:
        assert not (await session.scalar(select(Violation))).warning_active


async def test_bot_failed_task_retry_and_backup_are_available_without_web(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    async with database.transaction() as session:
        session.add(
            Action(
                id="failed-task",
                kind="warn",
                dedupe_key="failed-task",
                group_id=-1001,
                payload={"text": "重试通知"},
                state="failed",
                error="暂时离线",
            )
        )
    await deliver(runtime, {"message": private("/admin")})
    await click(runtime, telegram, "系统状态")
    await click(runtime, telegram, "查看失败任务")
    query = await click(runtime, telegram, "重试")
    await drain(service)
    await deliver(runtime, {"callback_query": query})
    await drain(service)
    assert texts(telegram).count("重试通知") == 1
    await click(runtime, telegram, "返回系统状态")
    query = await click(runtime, telegram, "生成一致性备份")
    await deliver(runtime, {"callback_query": query})
    assert len(list(env[0].backup_dir.glob("*.sqlite3"))) == 1


async def test_bot_captcha_callbacks_still_reach_verification_handler(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    await service.member_event(join())
    await drain(service)
    async with database.sessions() as session:
        verification = await session.scalar(select(Verification))
        answers = captcha.keyboard(verification.id, verification.nonce, verification.choices)
        data = next(button["callback_data"] for row in answers["inline_keyboard"]
                    for button in row if button["text"] == "123456")
    runtime = Runtime(database, telegram, service)
    query = {"id": "captcha-through-dispatcher", "from": {"id": 42}, "chat_instance": "group", "data": data}
    await deliver(runtime, {"callback_query": query})
    async with database.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "releasing"


async def test_ready_does_not_require_optional_web_oidc(env):
    settings, database, telegram, _ = env
    settings.bot_token = "999:FAKE_FOR_LOCAL_TESTS"
    settings.superadmin_ids = []
    app = create_app(settings, database, telegram, start_runtime=False)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["login_configured"] is False
        assert (await client.get("/auth/telegram/start")).status_code == 503


async def test_bot_command_menu_has_private_and_group_admin_scopes():
    from unittest.mock import AsyncMock

    gateway = TelegramGateway("")
    gateway.call = AsyncMock(return_value=True)
    await gateway.register_commands()
    calls = gateway.call.call_args_list
    assert [c.kwargs["scope"]["type"] for c in calls] == [
        "default",
        "all_private_chats",
        "all_chat_administrators",
    ]
    assert any(command["command"] == "admin" for command in calls[1].kwargs["commands"])


async def test_dispatcher_member_and_edited_ad_events_keep_telegram_field_names(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await deliver(runtime, {"chat_member": join()})
    await drain(service)
    async with database.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "pending"
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="广告"))
    await deliver(runtime, {"edited_message": message(501, "编辑后加广告", 43)})
    await drain(service)
    async with database.sessions() as session:
        violation = await session.scalar(select(Violation))
        assert violation.user_id == 43 and violation.message_id == 501
    assert any(m == "delete_message" and p["message_id"] == 501 for m, p in telegram.calls)


async def test_bot_reissue_ban_unban_and_other_bot_addressing(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    telegram.username = "PurrAITestBot"
    await service.member_event(join())
    await drain(service)
    async with database.sessions() as session:
        old_nonce = (await session.scalar(select(Verification))).nonce
    await deliver(runtime, {"message": message(600, "/reissue@PurrAITestBot 42 换一张", 1)})
    await drain(service)
    async with database.sessions() as session:
        row = await session.scalar(select(Verification))
        assert row.state == "pending" and row.nonce != old_nonce
    await deliver(runtime, {"message": message(601, "/ban@OtherBot 42 忽略", 1)})
    await drain(service)
    assert not any(m == "ban_chat_member" for m, p in telegram.calls)
    await deliver(runtime, {"message": message(602, "/ban 42 核实后暂时移出", 1)})
    await drain(service)
    assert telegram.members[(-1001, 42)]["status"] == "kicked"
    await deliver(runtime, {"message": message(603, "/unban 42 允许重新入群", 1)})
    await drain(service)
    assert telegram.members[(-1001, 42)]["status"] == "left"


async def test_failed_inbox_can_be_requeued_from_bot_system_panel(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    async with database.transaction() as session:
        session.add(Inbox(id=20, payload={"update_id": 20}, state="failed", error="离线", attempts=8))
    await deliver(runtime, {"message": private("/admin")})
    await click(runtime, telegram, "系统状态")
    await click(runtime, telegram, "查看失败事件")
    await click(runtime, telegram, "重试")
    async with database.sessions() as session:
        row = await session.get(Inbox, 20)
        assert row.state == "pending" and row.attempts == 0 and not row.error
