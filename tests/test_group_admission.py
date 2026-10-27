import pytest
from sqlalchemy import select

from purrai.management import Management, ManagementError
from purrai.models import Group
from purrai.runtime import Runtime
from test_bot_admin import click, deliver, markup, private, texts


TARGET = -1003792975859


def chat_api(monkeypatch, tg, *, kind="supergroup", status="administrator", failure=None):
    original = tg.call

    async def call(method, **kwargs):
        if kwargs.get("chat_id") == TARGET:
            if failure:
                raise failure
            if method == "get_chat":
                return {"id": TARGET, "type": kind, "title": "私密测试群"}
            if method == "get_chat_member":
                return {"status": status}
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", call)


async def test_owner_can_admit_undiscovered_group_through_api(env, client, monkeypatch):
    _, db, tg, _ = env
    chat_api(monkeypatch, tg)
    response = await client.post("/api/groups", json={"group_id": TARGET})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["title"] == "私密测试群" and result["approved"]
    assert not result["enabled"] and not result["settings"]["chat_enabled"]
    async with db.sessions() as session:
        assert (await session.get(Group, TARGET)).type == "supergroup"


async def test_readmitting_group_preserves_existing_features_and_settings(env, monkeypatch):
    settings, db, tg, service = env
    async with db.transaction() as session:
        source = await session.get(Group, -1001)
        session.add(Group(id=TARGET, title="旧群名", approved=True, enabled=True, status="active",
                          settings={**source.settings, "chat_enabled": True, "chat_persona": "自定义"}))
    chat_api(monkeypatch, tg)
    result = await Management(db, tg, service, settings).admit_group(1, TARGET)
    assert result["title"] == "私密测试群" and result["status"] == "active"
    assert result["enabled"] and result["settings"]["chat_enabled"]
    assert result["settings"]["chat_persona"] == "自定义"


@pytest.mark.parametrize("kind,status,error", [
    ("channel", "administrator", "只能接入群组"),
    ("private", "member", "只能接入群组"),
    ("supergroup", "left", "尚未加入"),
    ("supergroup", "kicked", "尚未加入"),
])
async def test_invalid_targets_are_not_admitted(env, monkeypatch, kind, status, error):
    settings, db, tg, service = env
    chat_api(monkeypatch, tg, kind=kind, status=status)
    with pytest.raises(ManagementError, match=error):
        await Management(db, tg, service, settings).admit_group(1, TARGET)
    async with db.sessions() as session:
        assert await session.get(Group, TARGET) is None


async def test_unavailable_telegram_does_not_create_placeholder(env, client, monkeypatch):
    _, db, tg, _ = env
    chat_api(monkeypatch, tg, failure=ConnectionError("offline"))
    response = await client.post("/api/groups", json={"group_id": TARGET})
    assert response.status_code == 503
    async with db.sessions() as session:
        assert await session.get(Group, TARGET) is None


async def test_only_superadmins_can_admit_and_api_rejects_invalid_ids(env, client):
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    with pytest.raises(ManagementError, match="超级管理员"):
        await management.admit_group(7, TARGET)
    assert tg.calls == []
    for value in (0, 123, -(2**63)-1, "https://t.me/+test"):
        assert (await client.post("/api/groups", json={"group_id": value})).status_code == 422
    settings.superadmin_ids = []
    assert (await client.post("/api/groups", json={"group_id": TARGET})).status_code == 403


async def test_bot_owner_can_enter_group_id_retry_invalid_input_and_open_group(env, monkeypatch):
    _, db, tg, service = env
    chat_api(monkeypatch, tg)
    runtime = Runtime(db, tg, service)
    try:
        await deliver(runtime, {"message": private("/admin")})
        await click(runtime, tg, "接入群组")
        await deliver(runtime, {"message": private("https://t.me/+test")})
        assert "负数数字 ID" in texts(tg)[-1]
        assert await runtime.admin.recall("bot_prompt:1")
        await deliver(runtime, {"message": private(str(TARGET))})
        assert await runtime.admin.recall("bot_prompt:1") is None
        assert "私密测试群" in str(markup(tg))
        async with db.sessions() as session:
            assert (await session.get(Group, TARGET)).approved
    finally:
        await runtime.stop()


async def test_regular_group_admin_cannot_open_or_reuse_admission_prompt(env):
    _, db, tg, service = env
    tg.members[(-1001, 7)] = {"status": "administrator"}
    runtime = Runtime(db, tg, service)
    try:
        await deliver(runtime, {"message": private("/admin", 7)})
        assert "接入群组" not in str(markup(tg))
        with pytest.raises(ManagementError, match="超级管理员"):
            await runtime.admin.inputs.admission_prompt(7)
        await runtime.admin.inputs.admission_prompt(1)
        service.settings.superadmin_ids = []
        await deliver(runtime, {"message": private(str(TARGET))})
        assert "超级管理员" in texts(tg)[-1]
        async with db.sessions() as session:
            assert await session.scalar(select(Group).where(Group.id == TARGET)) is None
    finally:
        await runtime.stop()
