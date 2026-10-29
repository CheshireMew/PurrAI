import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.management import Management
from purrai.models import Group, Member, SharedPolicy, Violation
from purrai.private_access import discover
from purrai.schemas import RuleInput, SettingsPatch, SharedPolicyPatch
from purrai.shared_policy import effective_rules
from test_profile_trust import profile_api


async def test_all_settings_inherit_and_explicit_equal_value_is_still_local(env):
    settings, db, tg, svc = env
    manager = Management(db, tg, svc, settings)
    await manager.settings_patch(1, -1001, SettingsPatch(settings={"chat_name": "柴郡喵", "repeat_count": 7}))
    await manager.configuration.shared(1, SharedPolicyPatch(
        settings={"chat_name": "通用昵称", "chat_persona": "全群通用人设", "repeat_count": 9,
                  "verification_timeout": 240, "archive_enabled": False}, request_id="shared-settings-1"))
    async with db.sessions() as session:
        group = await session.get(Group, -1001)
        assert group.settings["chat_name"] == "柴郡喵"
        assert group.settings["repeat_count"] == 7
        assert group.settings["chat_persona"] == "全群通用人设"
        assert group.settings["verification_timeout"] == 240
        assert not group.settings["archive_enabled"]
    async with db.transaction() as session:
        new = await discover(session, {"id": -1002, "title": "新群", "type": "supergroup"})
        assert new.settings["chat_name"] == "通用昵称" and new.settings["repeat_count"] == 9
    await manager.settings_patch(1, -1001, SettingsPatch(reset=["chat_name", "repeat_count"]))
    async with db.sessions() as session:
        group = await session.get(Group, -1001)
        assert group.settings["chat_name"] == "通用昵称" and group.settings["repeat_count"] == 9
        assert group.setting_overrides == []


async def test_shared_rules_apply_to_all_groups_and_edits_only_override_current_group(env, client):
    settings, db, tg, svc = env
    manager = Management(db, tg, svc, settings)
    result = await manager.configuration.shared(1, SharedPolicyPatch(
        rules=[{"kind": "keyword", "value": "通用测试词", "penalty": "permanent_mute"}], request_id="shared-rules-1"))
    identifier = result["rules"][0]["id"]
    async with db.transaction() as session:
        group = await discover(session, {"id": -1002, "title": "第二群", "type": "supergroup"})
        group.approved, group.enabled = True, True
    await manager.rule(1, -1001, RuleInput(kind="keyword", value="本群测试词", enabled=False), identifier)
    local = await client.get('/api/groups/-1001/rules')
    assert local.status_code == 200 and local.json()[0]["id"] == identifier and not local.json()[0]["enabled"]
    async with db.sessions() as session:
        assert (await effective_rules(session, -1002))[0]["value"] == "通用测试词"
        assert (await session.get(SharedPolicy, 1)).rules[0]["enabled"]
    incoming = message(1, "通用测试词")
    incoming["chat"]["id"] = -1002
    await svc.message(incoming)
    await drain(svc)
    assert tg.members[(-1002, 42)]["until_date"] == 0
    assert not tg.members[(-1002, 42)]["can_send_messages"]
    detail = await manager.queries.single(1, -1001, "rule", identifier)
    assert detail.group_id == -1001 and detail.value == "本群测试词"
    await manager.rules.inherit(1, -1001, identifier)
    async with db.sessions() as session:
        assert (await effective_rules(session, -1001))[0]["value"] == "通用测试词"


async def test_emoji_lists_are_group_local_and_old_members_mute_on_first_observed_message(env, monkeypatch):
    settings, db, tg, svc = env
    await profile_api(tg, monkeypatch, pack="TGVIP_16")
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, joined_at=0, join_observed=False))
        other = await discover(session, {"id": -1002, "title": "第二群", "type": "supergroup"})
        other.approved, other.enabled = True, True
        other.settings = {**other.settings, "emoji_status_blocked_packs": []}
    incoming = message(77, "正常发言")
    incoming["from"]["is_premium"] = True
    await svc.message(incoming)
    await drain(svc)
    assert tg.members[(-1001, 42)]["status"] == "restricted"
    assert tg.members[(-1001, 42)]["until_date"] == 0
    assert not tg.members[(-1001, 42)]["can_send_messages"]
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.get(Member, (-1001, 42))).present
        assert (await session.scalar(select(Violation))).profile_violation
    tg.calls.clear()
    incoming["chat"]["id"] = -1002
    await svc.message(incoming)
    await drain(svc)
    assert not any(method in ("restrict_chat_member", "ban_chat_member") for method, _ in tg.calls)


async def test_shared_updates_reject_stale_baseline_and_bad_rule_ids(env, client):
    baseline = (await client.get('/api/settings/defaults')).json()
    response = await client.patch('/api/settings/defaults', json={
        "settings": {"chat_persona": "新通用人设"}, "baseline": baseline, "request_id": "shared-baseline-1"})
    assert response.status_code == 200
    response = await client.patch('/api/settings/defaults', json={
        "settings": {"chat_persona": "旧页面修改"}, "baseline": baseline, "request_id": "shared-baseline-2"})
    assert response.status_code == 409
    response = await client.patch('/api/settings/defaults', json={
        "rules": [{"id": "missing", "kind": "keyword", "value": "错误"}], "request_id": "shared-bad-rule-1"})
    assert response.status_code == 422


async def test_ordinary_admin_cannot_change_shared_policy(env):
    settings, db, tg, svc = env
    tg.members[(-1001, 7)] = {"status": "administrator"}
    from purrai.errors import ManagementError
    with pytest.raises(ManagementError):
        await Management(db, tg, svc, settings).configuration.shared(7, SharedPolicyPatch(
            settings={"chat_name": "越权修改"}, request_id="shared-no-access"))
