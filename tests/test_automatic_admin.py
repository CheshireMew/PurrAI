import hashlib
import time

import pytest
from sqlalchemy import func, select

from purrai.management import ManagementError
from purrai.models import Admin, Grant, Group, Rule, Session, default_settings
from purrai.runtime import Runtime
from purrai.schemas import RuleInput
from test_bot_admin import click, deliver, markup, open_group, private, texts


@pytest.mark.parametrize("status", ["administrator", "creator"])
async def test_group_admin_can_activate_configure_and_use_bot_without_registration_or_global_owner(
    env, status
):
    settings, database, telegram, service = env
    settings.superadmin_ids = []
    telegram.members[(-1001, 2)] = {"status": status}
    async with database.transaction() as session:
        group = await session.get(Group, -1001)
        group.enabled, group.status = False, "pending"
        session.add(Group(approved=True, id=-1002, title="其他群", settings=default_settings()))
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram, 2)
    assert "系统状态" not in str(markup(telegram))
    await click(runtime, telegram, "检查并启用群保护", 2)
    await click(runtime, telegram, "验证设置", 2)
    await click(runtime, telegram, "图片验证", 2)
    async with database.sessions() as session:
        group = await session.get(Group, -1001)
        assert group.enabled and not group.settings["verification_enabled"]
        assert await session.scalar(select(func.count()).select_from(Admin)) == 0
        assert await session.scalar(select(func.count()).select_from(Grant)) == 0
    assert [g["id"] for g in await runtime.management.groups(2)] == [-1001]
    with pytest.raises(ManagementError, match="不是此群"):
        await runtime.management.rule(2, -1002, RuleInput(kind="keyword", value="广告"))
    with pytest.raises(ManagementError, match="仅限超级管理员"):
        await runtime.management.backup(2)


@pytest.mark.parametrize("status", ["member", "restricted", "left", "kicked"])
async def test_legacy_grants_cannot_authorize_non_admins(env, status):
    _, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Admin(id=2, active=True))
        await session.flush()
        session.add(Grant(admin_id=2, group_id=-1001))
    telegram.members[(-1001, 2)] = {"status": status}
    runtime = Runtime(database, telegram, service)
    with pytest.raises(ManagementError):
        await runtime.management.authorize(2, -1001)
    assert await runtime.management.groups(2) == []


async def test_old_inactive_manual_record_does_not_override_current_telegram_admin_role(env):
    _, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Admin(id=2, active=False))
    telegram.members[(-1001, 2)] = {"status": "administrator"}
    runtime = Runtime(database, telegram, service)
    assert await runtime.management.principal(2) is False
    await runtime.management.setting(2, -1001, "max_attempts", 4, "auto-admin-setting")
    async with database.sessions() as session:
        assert (await session.get(Group, -1001)).settings["max_attempts"] == 4


async def test_id_command_returns_real_sender_number_before_any_setup_and_rejects_anonymous_identity(env):
    settings, database, telegram, service = env
    settings.superadmin_ids = []
    runtime = Runtime(database, telegram, service)
    incoming = private("/id 999999", 234567890)
    incoming["from"]["username"] = "CheshireMew"
    await deliver(runtime, {"message": incoming})
    assert "数字 ID：234567890" in texts(telegram)[-1]
    assert "用户名：@CheshireMew" in texts(telegram)[-1]
    assert "999999" not in texts(telegram)[-1]
    assert not any(method == "get_chat_member" for method, _ in telegram.calls)
    incoming = private("/id", 1087968824)
    incoming["chat"] = {"id": -1001, "type": "supergroup"}
    incoming["sender_chat"] = {"id": -1001, "type": "supergroup", "title": "测试群"}
    await deliver(runtime, {"message": incoming})
    assert "匿名身份无法识别真实账号" in texts(telegram)[-1]
    assert "数字 ID：1087968824" not in texts(telegram)[-1]


async def test_telegram_role_promotion_and_revocation_refresh_existing_bot_buttons(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await deliver(runtime, {"message": private("/admin", 2)})
    assert "不是机器人已加入群的管理员" in texts(telegram)[-1]
    telegram.members[(-1001, 2)] = {"status": "administrator"}
    await open_group(runtime, telegram, 2)
    await click(runtime, telegram, "验证设置", 2)
    telegram.members[(-1001, 2)] = {"status": "member"}
    # Still admin of another group: entry access must not authorize the old group button.
    async with database.transaction() as session:
        session.add(Group(approved=True, id=-1002, title="仍在管理的群", settings=default_settings()))
    telegram.members[(-1002, 2)] = {"status": "administrator"}
    await click(runtime, telegram, "图片验证", 2)
    assert "不是此群" in texts(telegram)[-1]
    async with database.sessions() as session:
        assert (await session.get(Group, -1001)).settings["verification_enabled"]
    await deliver(runtime, {"message": private("/admin", 2)})
    assert "测试群" not in str(markup(telegram))
    assert "仍在管理的群" in str(markup(telegram))


async def test_existing_web_session_uses_current_group_roles_on_reads_writes_and_global_actions(env, client):
    _, database, telegram, _ = env
    async with database.transaction() as session:
        session.add(Group(approved=True, id=-1002, title="其他群", settings=default_settings()))
        session.add(
            Session(
                id=hashlib.sha256(b"auto-session").hexdigest(),
                user_id=2,
                csrf="auto-csrf",
                expires_at=time.time() + 3600,
            )
        )
    telegram.members[(-1001, 2)] = {"status": "administrator"}
    client.cookies.set("purrai_session", "auto-session")
    client.headers["X-CSRF-Token"] = "auto-csrf"
    me = (await client.get("/api/me")).json()
    assert me["id"] == 2 and not me["root"] and me["group_ids"] == [-1001]
    assert (await client.post("/api/groups/-1001/activate")).status_code == 200
    assert (
        await client.patch("/api/groups/-1001/settings", json={"settings": {"max_attempts": 4}})
    ).status_code == 200
    assert (await client.get("/api/groups/-1002/rules")).status_code == 403
    assert (await client.post("/api/groups/-1002/activate")).status_code == 403
    assert (await client.get("/api/system")).status_code == 403
    assert (await client.post("/api/system/backup")).status_code == 403
    telegram.members[(-1001, 2)] = {"status": "member"}
    telegram.members[(-1002, 2)] = {"status": "administrator"}
    assert (await client.get("/api/me")).json()["group_ids"] == [-1002]
    assert (await client.get("/api/groups/-1001/verifications")).status_code == 403
    assert (
        await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": "广告"})
    ).status_code == 403
    telegram.members[(-1002, 2)] = {"status": "member"}
    assert (await client.get("/api/me")).status_code == 403


async def test_unavailable_telegram_does_not_allow_automatic_management(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    telegram.members[(-1001, 2)] = {"status": "administrator"}
    telegram.fail["get_chat_member"] = 10
    with pytest.raises(ManagementError) as error:
        await runtime.management.rule(2, -1001, RuleInput(kind="keyword", value="广告"))
    assert error.value.status == 503
    with pytest.raises(ManagementError) as error:
        await runtime.management.groups(2)
    assert error.value.status == 503
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Rule)) == 0


async def test_manual_grant_api_is_retired_and_global_owner_still_works(client):
    response = await client.get("/api/admins")
    assert response.json()["access_mode"] == "telegram_group_admin"
    assert response.json()["superadmin_ids"] == [1]
    response = await client.put("/api/admins", json={"id": 2, "group_ids": [-1001]})
    assert response.status_code == 410 and "自动识别" in response.json()["detail"]
    assert (await client.get("/api/system")).status_code == 200
