"""Behavioral acceptance for the November functional completeness work."""

import copy
import json

import pytest
from sqlalchemy import func, select

from conftest import drain, message
from purrai.archive import capture, detail, page
from purrai.management import Management
from purrai.models import Group, Meta, PromotionSource, Rule, RuleHit, SharedPolicy, Violation
from purrai.schemas import ArchiveFilter, GroupSettings, PreviewInput
from purrai.shared_policy import effective_rules


async def test_learning_obeys_effective_local_domain_and_account_exceptions(env):
    _, db, _, service = env
    async with db.transaction() as session:
        session.add_all([
            Rule(group_id=-1001, kind="allow_domain", value="trusted.example"),
            Rule(group_id=-1001, kind="allow_mention", value="trusted_user"),
        ])
    await service.message(message(401, "免费带单，私聊我 @trusted_user @ad_target https://trusted.example/post"))
    async with db.sessions() as session:
        rules = await effective_rules(session, -1002)
        targets = {row["value"] for row in rules if row["kind"] == "block_target"}
        assert targets == {"account:ad_target"}
        sources = (await session.scalars(select(PromotionSource))).all()
        assert len(sources) == 1 and sources[0].message_id == 401 and sources[0].violation_id


async def test_unrelated_shared_edits_merge_with_new_learned_rules_and_real_conflicts_reject(env, client):
    _, db, _, service = env
    baseline = (await client.get('/api/settings/defaults')).json()
    await service.message(message(402, "免费带单，私聊我 @ad_target"))
    changed = await client.patch('/api/settings/defaults', json={"settings": {"chat_persona": "独立修改"},
        "baseline": baseline, "request_id": "independent-shared"})
    assert changed.status_code == 200 and len(changed.json()["rules"]) == 1
    conflict = await client.patch('/api/settings/defaults', json={"settings": {"chat_persona": "旧稿"},
        "baseline": baseline, "request_id": "conflicting-shared"})
    assert conflict.status_code == 409
    async with db.sessions() as session:
        assert (await session.get(SharedPolicy, 1)).rules[0]["origin"] == "learned"


async def test_rule_three_way_merge_preserves_concurrent_additions_and_metadata(env, client):
    _, db, _, service = env
    baseline = (await client.patch('/api/settings/defaults', json={"rules": [
        {"kind": "keyword", "value": "广告词"}], "request_id": "first-shared-rule"})).json()
    draft = copy.deepcopy(baseline["rules"])
    draft[0]["enabled"] = False
    await service.message(message(403, "免费带单，私聊我 @ad_target"))
    result = await client.patch('/api/settings/defaults', json={"rules": draft,
        "baseline": baseline, "request_id": "merge-rule-edit"})
    assert result.status_code == 200
    assert len(result.json()["rules"]) == 2
    assert not result.json()["rules"][0]["enabled"]
    assert result.json()["rules"][1]["origin"] == "learned"


async def test_archive_gap_is_visible_idempotent_and_survives_recording_resumption(env, client):
    settings, db, _, _ = env
    settings.archive_max_revisions = 1
    original = message(410, "初版")
    skipped = {**original, "text": "未保存的编辑", "edit_date": original["date"] + 10}
    async with db.transaction() as session:
        await capture(session, original, update_id=100, settings=settings)
        await capture(session, skipped, True, 101, settings=settings)
        await capture(session, skipped, True, 101, settings=settings)
        await capture(session, original, update_id=100, settings=settings)
    row = (await page(db, -1001, ArchiveFilter()))["items"][0]
    assert row["text"] == "初版"
    assert row["recording_gap"]["omitted_at_least"] == 1
    assert row["recording_gap"]["latest_not_recorded"]
    settings.archive_max_revisions = 3
    async with db.transaction() as session:
        await capture(session, {**skipped, "text": "恢复记录", "edit_date": original["date"] + 20},
                      True, 102, settings=settings)
    saved = await detail(db, -1001, row["id"])
    assert saved["text"] == "恢复记录" and not saved["recording_gap"]["latest_not_recorded"]
    exported = await client.get('/api/groups/-1001/messages/export')
    assert json.loads(exported.text)["recording_gap"]["omitted_at_least"] == 1


async def test_dashboard_excludes_broken_and_unapproved_enabled_groups(env, client):
    _, db, _, _ = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.status = "error"
        session.add(Group(id=-1002, approved=False, enabled=True, status="active"))
    data = (await client.get('/api/dashboard')).json()
    assert data["active_groups"] == 0 and data["abnormal_groups"] == 1


async def test_rule_search_counts_unique_messages_and_bulk_override_is_group_local(env, client):
    settings, db, tg, service = env
    manager = Management(db, tg, service, settings)
    created = await client.patch('/api/settings/defaults', json={"rules": [
        {"kind": "keyword", "value": "推广词"}, {"kind": "keyword", "value": "另一词"}],
        "request_id": "rule-query-seed"})
    assert created.status_code == 200
    await service.message(message(420, "推广词"))
    await service.message(message(420, "推广词"))
    result = (await client.get('/api/groups/-1001/rules/query?query=推广&source=shared&size=1')).json()
    assert result["total"] == 1 and result["items"][0]["hit_count"] == 1
    item = result["items"][0]
    changed = await client.post('/api/groups/-1001/rules/batch', json={"enabled": False,
        "versions": {item["id"]: item["version"]}, "request_id": "bulk-disable-local"})
    assert changed.status_code == 200
    async with db.sessions() as session:
        assert not next(rule for rule in await effective_rules(session, -1001) if rule["id"] == item["id"])["enabled"]
        assert next(rule for rule in await effective_rules(session, -1002) if rule["id"] == item["id"])["enabled"]
        assert await session.scalar(select(func.count()).select_from(RuleHit)) == 1
    # Draft previews overlay the complete policy, not just the displayed page.
    preview = await manager.preview(1, -1001, PreviewInput(
        text="另一词", draft={"kind": "keyword", "value": "新规则"}))
    assert "关键词：另一词" in preview["reasons"]
    stale = await client.post('/api/groups/-1001/rules/batch', json={"enabled": True,
        "versions": {item["id"]: item["version"]}, "request_id": "bulk-stale-change"})
    assert stale.status_code == 409


@pytest.mark.parametrize("scope", ["local", "global"])
async def test_correction_previews_targets_and_atomically_unbans_with_explicit_scope(env, client, scope):
    _, db, tg, service = env
    await service.message(message(430, "免费带单，私聊我 @ad_target"))
    await drain(service)
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
        original = list(violation.reasons)
    url = f'/api/groups/-1001/violations/{violation.id}/correction'
    evidence = (await client.get(url)).json()
    rule = evidence["rules"][0]
    assert evidence["global_allowed"] and rule["consequence"].startswith("删除消息并永久封禁")
    payload = {"reason": "已核实误判", "action": "unban", "scope": scope,
               "rule_versions": {rule["id"]: rule["global_version"] if scope == "global" else rule["version"]},
               "request_id": "correct-with-evidence"}
    response = await client.post(url, json=payload)
    assert response.status_code == 200, response.text
    assert (await client.post(url, json=payload)).json() == response.json()
    await drain(service)
    assert tg.members[(-1001, 42)]["status"] == "left"
    async with db.sessions() as session:
        assert (await session.get(Violation, violation.id)).reasons == original
        assert not (await effective_rules(session, -1001))[0]["enabled"]
        assert (await effective_rules(session, -1002))[0]["enabled"] == (scope == "local")
        assert (await session.scalar(select(PromotionSource))).withdrawn_at > 0
        assert await session.get(Meta, f"violation-correction:{violation.id}")


async def test_correction_rejects_changed_rule_without_partial_source_or_penalty_changes(env, client):
    settings, db, tg, service = env
    await service.message(message(431, "免费带单，私聊我 @ad_target"))
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
    url = f'/api/groups/-1001/violations/{violation.id}/correction'
    rule = (await client.get(url)).json()["rules"][0]
    manager = Management(db, tg, service, settings)
    await manager.rules.patch_rule(1, -1001, rule["id"], {"enabled": False}, "later-rule-edit", rule["version"])
    response = await client.post(url, json={"reason": "过期纠正", "rule_versions": {rule["id"]: rule["version"]},
        "request_id": "stale-correction"})
    assert response.status_code == 409
    async with db.sessions() as session:
        assert not (await session.scalar(select(PromotionSource))).withdrawn_at
        assert not await session.get(Meta, f"violation-correction:{violation.id}")


async def test_setting_inheritance_supports_individual_reset_and_rejects_stale_reset(env, client):
    state = (await client.get('/api/groups/-1001/settings/inheritance')).json()
    assert 'chat_name' not in state['overrides']
    changed = await client.patch('/api/groups/-1001/settings', json={"settings": {"chat_name": "小橘", "max_attempts": 5}})
    assert changed.status_code == 200
    stale = await client.patch('/api/groups/-1001/settings', json={"reset": ["chat_name"],
        "baseline": {"chat_name": state['settings']['chat_name']}})
    assert stale.status_code == 409
    restored = await client.patch('/api/groups/-1001/settings', json={"reset": ["chat_name"],
        "baseline": {"chat_name": "小橘"}})
    assert restored.status_code == 200
    assert restored.json()["settings"]["chat_name"] == GroupSettings().chat_name
    assert restored.json()["settings"]["max_attempts"] == 5
    assert restored.json()["setting_overrides"] == ["max_attempts"]


async def test_bot_common_setting_and_target_rule_work_without_web(env):
    from purrai.runtime import Runtime
    from test_bot_admin import click, deliver, private, texts

    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await deliver(runtime, {"message": private('/admin')})
    await click(runtime, tg, '通用设置与规则')
    await click(runtime, tg, '通用群设置')
    await click(runtime, tg, '聊天昵称')
    await click(runtime, tg, '修改此项')
    await deliver(runtime, {"message": private('小橘')})
    async with db.sessions() as session:
        assert (await session.get(SharedPolicy, 1)).settings['chat_name'] == '小橘'
        assert (await session.get(Group, -1001)).settings['chat_name'] == '小橘'
    await deliver(runtime, {"message": private('/admin')})
    await click(runtime, tg, '通用设置与规则')
    await click(runtime, tg, '通用广告规则与推广目标')
    await click(runtime, tg, '新增通用规则')
    await click(runtime, tg, '推广目标黑名单')
    await deliver(runtime, {"message": private('@persistent_target')})
    async with db.sessions() as session:
        rules = (await session.get(SharedPolicy, 1)).rules
        assert rules, texts(tg)[-3:]
        assert rules[0]['value'] == 'account:persistent_target'
    await click(runtime, tg, 'account:persistent_target')
    await click(runtime, tg, '停用通用规则')
    async with db.sessions() as session:
        assert not (await session.get(SharedPolicy, 1)).rules[0]['enabled']


async def test_bot_correction_requires_review_and_reason_before_disabling_global_rule(env):
    from purrai.runtime import Runtime
    from test_bot_admin import click, deliver, markup, open_group, private

    _, db, tg, service = env
    await service.message(message(440, '免费带单，私聊我 @ad_target'))
    await drain(service)
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, '违规记录')
    await click(runtime, tg, '用户 42')
    await click(runtime, tg, '纠正误判与关联黑名单')
    assert '确认保存纠正' not in str(markup(tg))
    await click(runtime, tg, '停用 account:ad_target')
    await click(runtime, tg, '切换为停用通用规则')
    await click(runtime, tg, '解除原封禁')
    await click(runtime, tg, '填写核实原因')
    await deliver(runtime, {"message": private('已核实误判，撤销推广目标')})
    async with db.sessions() as session:
        assert (await session.get(SharedPolicy, 1)).rules[0]['enabled']
    await click(runtime, tg, '确认保存纠正')
    await drain(service)
    async with db.sessions() as session:
        assert not (await session.get(SharedPolicy, 1)).rules[0]['enabled']
    assert tg.members[(-1001, 42)]['status'] == 'left'


async def test_dashboard_separates_advertising_spam_and_other_policy_records(env, client):
    _, db, _, _ = env
    async with db.transaction() as session:
        for message_id, reasons in [(451, ['明确广告引流：测试']), (452, ['近似内容刷屏']),
                                    (453, ['独立文字发言不足 3 条']), (454, ['关键词：测试', '重复刷屏'])]:
            session.add(Violation(group_id=-1001, message_id=message_id, user_id=42, reasons=reasons))
    data = (await client.get('/api/dashboard')).json()
    assert data['violations_today'] == 4
    assert data['advertising_today'] == 2 and data['spam_today'] == 2 and data['other_policy_today'] == 1


async def test_ordinary_admin_cannot_apply_global_correction(env):
    from purrai.errors import ManagementError
    from purrai.schemas import ViolationCorrectionInput

    settings, db, tg, service = env
    await service.message(message(455, '免费带单，私聊我 @ad_target'))
    tg.members[(-1001, 7)] = {'status': 'administrator'}
    manager = Management(db, tg, service, settings)
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
    evidence = await manager.corrections.preview(7, -1001, violation.id)
    assert not evidence['global_allowed'] and evidence['affected_groups'] == []
    rule = evidence['rules'][0]
    with pytest.raises(ManagementError, match='超级管理员'):
        await manager.corrections.apply(7, -1001, violation.id, ViolationCorrectionInput(reason='误判',
            scope='global', rule_versions={rule['id']: rule['global_version']}, request_id='root-only-correction'))
