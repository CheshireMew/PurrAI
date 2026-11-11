"""Acceptance of member, operations, archive and continuity workflows."""

from sqlalchemy import func, select

from conftest import join, message
from purrai.archive import capture, page
from purrai.chat_budget import ChatBudget
from purrai.management import Management
from purrai.models import Action, ArchivedMessage, Group, Member, Meta, Verification, Violation, now
from purrai.runtime import Runtime
from purrai.runtime_alerts import RuntimeAlerts
from purrai.schemas import ArchiveFilter


async def test_member_query_and_duration_are_separate_from_group_defaults(env, client):
    _, db, tg, _ = env
    detail = await client.get('/api/groups/-1001/members/42')
    assert detail.status_code == 200 and detail.json()['current']['status'] == 'member'
    result = await client.post('/api/groups/-1001/members/42/actions', json={
        'action': 'mute', 'reason': '本次单独半小时', 'duration_seconds': 1800, 'request_id': 'member-half-hour'})
    assert result.status_code == 200 and result.json()['payload']['duration_seconds'] == 1800
    again = await client.post('/api/groups/-1001/members/42/actions', json={
        'action': 'mute', 'reason': '本次单独半小时', 'duration_seconds': 1800, 'request_id': 'member-half-hour'})
    assert again.json()['id'] == result.json()['id']
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings['mute_seconds'] == 259200
    tg.fail['get_chat_member'] = 1
    unavailable = (await client.get('/api/groups/-1001/members/42')).json()
    assert unavailable['current'] is None and unavailable['current_error']


async def test_batch_snapshot_excludes_new_join_and_rejects_changed_verification(env, client):
    _, db, _, service = env
    await service.member_event(join(42))
    await service.member_event(join(43))
    preview = (await client.post('/api/groups/-1001/release-pending/preview', json={})).json()
    await service.member_event(join(44))
    selected = preview['items'][0]['id']
    exact = (await client.post('/api/groups/-1001/release-pending/preview', json={'verification_ids': [selected]})).json()
    ended = (await client.post('/api/groups/-1001/release-pending/preview', json={'state': 'failed'})).json()
    assert ended['total'] == 0
    body = {'action': 'release', 'reason': '只放行核实的成员', 'verification_ids': [selected],
            'snapshot': exact['snapshot'], 'request_id': 'batch-selection-test'}
    result = await client.post('/api/groups/-1001/release-pending', json=body)
    assert result.status_code == 200 and len(result.json()['action_ids']) == 1
    assert (await client.post('/api/groups/-1001/release-pending', json=body)).json() == result.json()
    async with db.sessions() as session:
        newcomer = await session.scalar(select(Verification).where(Verification.user_id == 44))
        assert newcomer.state == 'muting'
    stale = await client.post('/api/groups/-1001/release-pending', json={**body, 'request_id': 'batch-stale-test'})
    assert stale.status_code == 409


async def test_uncertain_delivery_review_is_audited_without_resending(env, client):
    _, db, tg, _ = env
    async with db.transaction() as session:
        session.add(Action(id='uncertain', kind='challenge', group_id=-1001, user_id=42,
            dedupe_key='uncertain', state='failed', error='验证码发送结果未确认',
            payload={'challenge_delivery': 'uncertain'}))
    count = len(tg.calls)
    result = await client.post('/api/actions/uncertain/resolve', json={
        'outcome': 'completed', 'reason': '已在 Telegram 核实原图片', 'request_id': 'review-delivery-test'})
    assert result.status_code == 200
    async with db.sessions() as session:
        row = await session.get(Action, 'uncertain')
        assert row.state == 'reviewed' and row.payload['challenge_delivery'] == 'uncertain'
        assert row.error == '验证码发送结果未确认'
    assert len(tg.calls) == count
    failure_page = (await client.get('/api/system/failures')).json()
    assert all(row['id'] != 'uncertain' for row in failure_page['items'])
    operations = (await client.get('/api/groups/-1001/actions?manual_only=false&query=核实')).json()
    assert any(row['kind'] == 'delivery_review' for row in operations['items'])


async def test_alerts_merge_repeated_faults_and_send_recovery_once(env):
    cfg, db, _, _ = env
    alerts = RuntimeAlerts(db, cfg)
    async with db.transaction() as session:
        (await session.get(Group, -1001)).status = 'error'
    await alerts.cycle()
    await alerts.cycle()
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Action).where(Action.kind == 'owner_alert')) == 1
        (await session.get(Group, -1001)).status = 'active'
    await alerts.cycle()
    await alerts.cycle()
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Action).where(Action.kind == 'owner_alert')) == 2


async def test_archive_export_bound_cleanup_and_quota_isolation(env, client):
    cfg, db, _, _ = env
    async with db.transaction() as session:
        session.add(Group(id=-1002, approved=True, enabled=True))
        await session.flush()
        await capture(session, message(501, '第一条归档'), settings=cfg)
        await capture(session, message(502, '第二条归档'), settings=cfg)
    quota = await client.patch('/api/groups/-1001/archive/capacity', json={
        'group_mb': 1, 'global_mb': 64, 'request_id': 'quota-isolation-test'})
    assert quota.status_code == 200
    capacity = (await client.get('/api/groups/-1001/archive/capacity')).json()
    assert capacity['group_limit_bytes'] == 1048576 and capacity['group_bytes'] > 0
    exported = await client.get('/api/groups/-1001/messages/export')
    export_id = exported.headers['x-purrai-export-id']
    async with db.transaction() as session:
        await capture(session, message(503, '导出之后的新消息'), settings=cfg)
    body = {'export_id': export_id, 'confirm_count': 2, 'reason': '已保存并核对文件', 'request_id': 'purge-exported-test'}
    cleaned = await client.post('/api/groups/-1001/archive/purge', json=body)
    assert cleaned.status_code == 200 and cleaned.json()['removed'] == 2
    assert (await client.post('/api/groups/-1001/archive/purge', json=body)).json() == cleaned.json()
    assert [row['message_id'] for row in (await page(db, -1001, ArchiveFilter()))['items']] == [503]
    exported = await client.get('/api/groups/-1001/messages/export')
    async with db.transaction() as session:
        await capture(session, {**message(503, '导出后被编辑'), 'edit_date': int(now()) + 1}, True, settings=cfg)
    rejected = await client.post('/api/groups/-1001/archive/purge', json={**body,
        'export_id': exported.headers['x-purrai-export-id'], 'confirm_count': 1, 'request_id': 'purge-changed-test'})
    assert rejected.status_code == 409


async def test_backup_exercise_is_independent_and_retention_keeps_foreign_files(env, client):
    cfg, db, tg, _ = env
    management = Management(db, tg, settings=cfg)
    first = await management.backups.create(1, 'first-owned-backup', exercise=True)
    assert first['exercise']['integrity_ok'] and first['exercise']['compatible']
    first_path = cfg.backup_dir / first['file']
    import shutil
    foreign = cfg.backup_dir / 'purrai-20200101-000000-000000.sqlite3'
    shutil.copyfile(first_path, foreign)
    exercise = await client.post('/api/system/backups/exercise', json={'file': foreign.name, 'request_id': 'exercise-foreign-test'})
    assert exercise.status_code == 200 and exercise.json()['foreign_keys_ok']
    await management.backups.create(1, 'second-owned-backup')
    from purrai.schemas import BackupPlanInput
    plan = BackupPlanInput(request_id='retention-authorization', prune_enabled=True, retention_count=1)
    await management.backups.plan(1, plan)
    await management.backups.prune(plan.model_dump(exclude={'request_id'}))
    assert foreign.exists() and not first_path.exists()
    assert (await db.schema_health())['ready']


async def test_chat_usage_is_idempotent_even_when_settlement_crosses_midnight(env, monkeypatch):
    cfg, db, tg, _ = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, 'chat_enabled': True}
    budget = ChatBudget(db, cfg)
    monkeypatch.setattr(budget, 'day', lambda: '2026-11-10')
    # load() is a class method, so patch the class for the actual budget roll.
    monkeypatch.setattr(ChatBudget, 'day', staticmethod(lambda: '2026-11-10'))
    reservation = await budget.reserve(-1001, 42, '提示词', '输入')
    monkeypatch.setattr(ChatBudget, 'day', staticmethod(lambda: '2026-11-11'))
    await budget.settle(reservation, 123)
    await budget.settle(reservation, 123)
    management = Management(db, tg, settings=cfg)
    data = await management.chat_admin.diagnostics(1, -1001)
    assert data['usage'][0]['tokens'] == 123 and data['usage'][0]['calls'] == 1
    assert (await budget.status(-1001))['group_calls'] == 0


async def test_archived_draft_replay_has_no_moderation_or_model_side_effects(env, client):
    cfg, db, tg, _ = env
    async with db.transaction() as session:
        await capture(session, message(510, '测试专用词'), settings=cfg)
        row = await session.scalar(select(ArchivedMessage))
        identifier = row.id
    calls = len(tg.calls)
    result = await client.post('/api/groups/-1001/rules/preview-archive', json={
        'archive_ids': [identifier], 'draft': {'kind': 'keyword', 'value': '测试专用词', 'scope': 'message'}})
    assert result.status_code == 200, result.text
    item = result.json()['items'][0]
    assert not item['before']['delete_message'] and item['after']['delete_message']
    assert len(tg.calls) == calls
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Violation)) == 0


async def test_supergroup_migration_preserves_configuration_members_history_and_reissues_safely(env):
    cfg, db, tg, service = env
    await service.member_event(join(42))
    async with db.transaction() as session:
        origin = await session.get(Group, -1001)
        origin.type = 'group'
        origin.setting_overrides = ['chat_name']
        origin.settings = {**origin.settings, 'chat_name': '原群昵称'}
        await capture(session, message(1, '原普通群消息'), settings=cfg)
    runtime = Runtime(db, tg, service)
    try:
        event = {'update_id': 900, 'message': {**message(99, ''), 'migrate_to_chat_id': -1009991}}
        await runtime.persist([event])
        await runtime.persist([event, {'update_id': 901, 'message': {'chat': {'id': -1009991, 'type': 'supergroup'}, 'migrate_from_chat_id': -1001}}])
        async with db.sessions() as session:
            target = await session.get(Group, -1009991)
            assert target.approved and target.enabled and target.settings['chat_name'] == '原群昵称'
            assert target.setting_overrides == ['chat_name']
            assert not (await session.get(Group, -1001)).approved
            assert await session.get(Member, (-1009991, 42))
            verification = await session.scalar(select(Verification))
            assert verification.group_id == -1009991 and verification.message_id is None
        async with db.transaction() as session:
            await capture(session, {**message(1, '新超级群编号同为一'), 'chat': {'id': -1009991, 'type': 'supergroup'}}, settings=cfg)
        records = (await page(db, -1009991, ArchiveFilter()))['items']
        assert len(records) == 2
        historical = next(row for row in records if row.get('origin_group_id'))
        assert historical['origin_group_id'] == -1001 and historical['origin_message_id'] == 1
        groups = await runtime.management.groups(1)
        assert [group['id'] for group in groups] == [-1009991]
        assert groups[0]['previous_ids'] == [-1001]
        from purrai.errors import ManagementError
        import pytest
        with pytest.raises(ManagementError, match='升级'):
            await runtime.management.authorize(1, -1001)
        with pytest.raises(ManagementError, match='升级'):
            await runtime.management.set_group_access(-1001, True, 1)
    finally:
        await runtime.admin.ui.close()


async def test_cross_account_spam_and_expiring_emergency_respect_original_settings(env, client, monkeypatch):
    _, db, _, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, 'coordinated_enabled': True, 'coordinated_count': 3,
            'raid_enabled': True, 'raid_count': 3, 'verification_enabled': False, 'emergency_seconds': 60}
    for user in (51, 52, 53):
        await service.message(message(user, '这是一段由多个账号重复发送的独立内容', user))
    async with db.sessions() as session:
        spam = await session.scalar(select(Violation).where(Violation.user_id == 53))
        assert spam and any('多账号重复刷屏' in reason for reason in spam.reasons)
    assert (await client.get('/api/dashboard')).json()['spam_today'] == 1
    for user in (61, 62, 63):
        await service.member_event(join(user))
    status = (await client.get('/api/groups/-1001/emergency')).json()
    assert status['active'] and not status['base_verification_enabled']
    async with db.sessions() as session:
        verification = await session.scalar(select(Verification).where(Verification.user_id == 63))
        assert verification
        assert not (await session.get(Group, -1001)).settings['verification_enabled']
    import purrai.coordinated_protection as protection
    monkeypatch.setattr(protection, 'now', lambda: status['until'] + 1)
    assert not (await client.get('/api/groups/-1001/emergency')).json()['active']
    restored = await client.post('/api/groups/-1001/emergency', json={
        'active': False, 'reason': '人工核实恢复原配置', 'request_id': 'emergency-restore-test'})
    assert restored.status_code == 200 and not restored.json()['active']


async def test_bot_selected_batch_preview_requires_reason_and_rejects_changed_nonce(env):
    from test_bot_admin import click, deliver, markup, open_group, private, texts
    _, db, tg, service = env
    for user in range(60, 70):
        await service.member_event(join(user))
    runtime = Runtime(db, tg, service)
    try:
        await open_group(runtime, tg)
        await click(runtime, tg, '待验证成员')
        await click(runtime, tg, '选择并预览批量放行名单')
        await click(runtime, tg, '下一页')
        assert '(68)' in str(markup(tg)) and '(69)' in str(markup(tg))
        await click(runtime, tg, '全部取消选择')
        await click(runtime, tg, '(60)')
        await click(runtime, tg, '填写放行原因')
        await deliver(runtime, {'message': private('已核对选中成员')})
        await click(runtime, tg, '核对并放行所选')
        await click(runtime, tg, '确认执行')
        async with db.sessions() as session:
            rows = (await session.scalars(select(Verification))).all()
            assert sum(row.state == 'releasing' for row in rows) == 1
            assert next(row for row in rows if row.user_id == 60).state == 'releasing'
        assert '已提交所选 1 人' in texts(tg)[-1]
        await open_group(runtime, tg)
        await click(runtime, tg, '待验证成员')
        await click(runtime, tg, '选择并预览批量放行名单')
        await click(runtime, tg, '填写放行原因')
        await deliver(runtime, {'message': private('核对后状态变化')})
        async with db.transaction() as session:
            changed = await session.scalar(select(Verification).where(Verification.user_id == 61))
            changed.nonce = 'later-nonce'
        await click(runtime, tg, '核对并放行所选')
        assert '验证已变化' in texts(tg)[-1]
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Verification).where(Verification.state == 'releasing')) == 1
    finally:
        await runtime.admin.ui.close()


async def test_bot_emergency_and_backup_plan_only_commit_after_confirmation(env):
    from test_bot_admin import click, deliver, open_group, private
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    try:
        await open_group(runtime, tg)
        await click(runtime, tg, '临时应急保护')
        await click(runtime, tg, '启用临时应急保护')
        await deliver(runtime, {'message': private('临时核实集中入群')})
        async with db.sessions() as session:
            assert not await session.get(Meta, 'emergency:-1001')
        await click(runtime, tg, '确认调整应急保护')
        assert (await runtime.management.protection.status(1, -1001))['active']
        await deliver(runtime, {'message': private('/admin')})
        await click(runtime, tg, '系统状态与备份')
        await click(runtime, tg, '备份计划、文件与演练')
        await click(runtime, tg, '修改计划与保留授权')
        await deliver(runtime, {'message': private('hour_beijing=4\nretention_count=3')})
        async with db.sessions() as session:
            assert not await session.get(Meta, 'backup:plan')
        await click(runtime, tg, '确认计划与保留授权')
        state = await runtime.management.backups.status(1)
        assert state['plan']['hour_beijing'] == 4 and state['plan']['retention_count'] == 3
        assert not state['plan']['prune_enabled'] and not state['plan']['enabled']
    finally:
        await runtime.admin.ui.close()


async def test_bot_common_settings_and_rule_edits_use_shared_policy(env):
    from test_bot_admin import click, deliver, private
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    try:
        await deliver(runtime, {'message': private('/admin')})
        await click(runtime, tg, '通用设置与规则')
        await click(runtime, tg, '通用群设置')
        await click(runtime, tg, '群聊天：')
        await click(runtime, tg, '改为开启')
        async with db.sessions() as session:
            assert (await session.get(Group, -1001)).settings['chat_enabled']
        await deliver(runtime, {'message': private('/admin')})
        await click(runtime, tg, '通用设置与规则')
        await click(runtime, tg, '通用广告规则与推广目标')
        await click(runtime, tg, '新增通用规则')
        await click(runtime, tg, '关键词')
        await deliver(runtime, {'message': private('通用测试广告词')})
        await click(runtime, tg, '通用测试广告词')
        await click(runtime, tg, '正文改为永久禁言')
        shared = await runtime.management.configuration.shared(1)
        assert shared['rules'][0]['value'] == '通用测试广告词'
        assert shared['rules'][0]['penalty'] == 'permanent_mute'
    finally:
        await runtime.admin.ui.close()
