"""Saved policy must govern real messages, not only matching previews."""
from purrai.policy_work import PolicyExecution
import asyncio

import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai import chat_commands
from purrai.action_queue import enqueue
from purrai.ai_advertising import advertisement_count, record_ai_advertisement
from purrai.management import Management
from purrai.models import Action, AdvertisementReview, Group, Member, SharedPolicy, now
from purrai.notice_cleanup import NoticeCleanup
from purrai.recognition_policy import RecognitionPolicy
from purrai.schemas import GroupSettings, RuleInput, SettingsPatch
from purrai.shared_policy import effective_rules
from test_advertisement_review import reviewer, records
from test_business_rules import arbitrary_rule
from test_chat import configured
from test_recognition_policy import save, preview


async def settings(env, **values):
    config, db, tg, svc = env
    await Management(db, tg, svc, config).settings_patch(1, -1001, SettingsPatch(settings=values))
    async with db.sessions() as session:
        return GroupSettings.model_validate((await session.get(Group, -1001)).settings)


async def rule(env, **values):
    config, db, tg, svc = env
    return await Management(db, tg, svc, config).rule(1, -1001, RuleInput(**values))


@pytest.mark.parametrize("section,first,second,sample,target", [
    ("phone_prefixes", "琥珀", "珊瑚", "{}13800138000", "phone:13800138000"),
    ("shortener_domains", "amber.example", "coral.example", "https://{}/offer", "url:https://amber.example/offer"),
])
async def test_recognition_configuration_survives_filtering_in_live_service(client, env, section, first, second, sample, target):
    await settings(env, external_group_ban=False, external_invite_ban=False)
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    document["recognition"] = RecognitionPolicy().model_dump()
    for stage in document["stages"]:
        stage["rules"] = []
    await rule(env, kind="block_target", value=target, penalty="delete")
    entry = {"id": "arbitrary", "title": "任意入口", "enabled": True, "value": first}
    document["recognition"][section] = [entry]
    for identifier, expected in [(1, True), (2, False), (3, True), (4, False), (5, False)]:
        if identifier == 2:
            entry["value"] = second
        if identifier == 3:
            entry["value"] = first
        if identifier == 4:
            entry["enabled"] = False
        if identifier == 5:
            document["recognition"][section] = []
        await save(client, document)
        text = sample.format(first)
        result = await preview(client, document, text)
        assert result["delete_message"] is expected
        await env[3].message(message(identifier, text, user_id=40 + identifier))
        await drain(env[3])
        assert any(method == "delete_message" and data["message_id"] == identifier
                   for method, data in env[2].calls) is expected
    assert not any(method in ("ban_chat_member", "restrict_chat_member") for method, _ in env[2].calls)


@pytest.mark.parametrize("text", ["/这是普通中文消息", "@arbitrary_bot random_token"])
async def test_slash_and_underscore_are_not_hidden_review_exemptions(client, env, text):
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    document["recognition"] = RecognitionPolicy().model_dump()
    await save(client, document)
    worker, model = await reviewer(env, lambda _: False)
    await env[3].message(message(701, text))
    assert (await records(env[1], AdvertisementReview))[0].state == "pending"
    assert await worker.run_one()
    assert model.calls[0][0]["text"] == text


async def test_cooldown_uses_explicit_flag_and_current_duration_not_reason(client, env, monkeypatch):
    chat, _ = await configured(env)
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    for stage in document["stages"]:
        stage["rules"] = []
    entry = arbitrary_rule("chat_solicitation", word="星云橙灯")
    entry["advertiser_cooldown"] = True
    next(stage for stage in document["stages"] if stage["id"] == "chat_solicitation")["rules"] = [entry]
    for identifier, reason, flag in [(1, "任意说明", True), (2, "广告招揽：任意说明", True),
                                     (3, "广告招揽：任意说明", False)]:
        entry.update(reason=reason, advertiser_cooldown=flag)
        await save(client, document)
        await chat.observe(message(identifier, "星云橙灯", user_id=41 + identifier))
        async with env[1].sessions() as session:
            _, state = await chat_commands.load(session, -1001)
        assert chat_commands.advertiser_blocked(state, 41 + identifier) is flag
    stamp = state["advertisers"]["42"]["at"]
    monkeypatch.setattr(chat_commands, "now", lambda: stamp + 61)
    cfg = await settings(env, chat_advertiser_cooldown_seconds=60)
    assert not chat_commands.advertiser_blocked(state, 42, cfg)
    cfg = await settings(env, chat_advertiser_cooldown_seconds=120)
    assert chat_commands.advertiser_blocked(state, 42, cfg)
    cfg = await settings(env, chat_advertiser_cooldown_seconds=0)
    assert not chat_commands.advertiser_blocked(state, 42, cfg)
    await chat.close()


@pytest.mark.parametrize("kind", ["block_target", "keyword"])
@pytest.mark.parametrize("penalty", ["delete", "permanent_mute", "ban"])
async def test_target_and_nickname_actions_are_managed_data(env, kind, penalty):
    await settings(env, external_group_ban=False, external_invite_ban=False)
    if kind == "block_target":
        await rule(env, kind=kind, value="phone:13800138000", penalty=penalty)
        item = message(801, "电话13800138000")
    else:
        await rule(env, kind=kind, value="星云橙灯", scope="name", name_penalty=penalty)
        item = message(801, "今天聊什么话题", **{"from": {"id": 42, "first_name": "星云橙灯"}})
    await env[3].message(item)
    await drain(env[3])
    calls = [method for method, _ in env[2].calls]
    assert "delete_message" in calls
    assert ("restrict_chat_member" in calls) is (penalty == "permanent_mute")
    assert ("ban_chat_member" in calls) is (penalty == "ban")


@pytest.mark.parametrize("kind,scope,value,text,name", [
    ("block_target", "message", "phone:13800138000", "电话13800138000", "普通成员"),
    ("keyword", "message", "星云橙灯", "星云橙灯", "普通成员"),
    ("keyword", "name", "星云橙灯", "普通聊天正文", "星云橙灯"),
])
@pytest.mark.parametrize("penalty", ["delete", "permanent_mute", "ban"])
async def test_disabling_rule_cancels_pending_deletion_and_member_penalty(env, kind, scope, value, text, name, penalty):
    await rule(env, kind=kind, scope=scope, value=value, penalty=penalty, name_penalty=penalty)
    await env[3].message(message(802, text, **{"from": {"id": 42, "first_name": name}}))
    config, db, tg, svc = env
    manager = Management(db, tg, svc, config)
    async with db.sessions() as session:
        current = (await effective_rules(session, -1001))[0]
    await manager.rules.patch_rule(1, -1001, current["id"], {"enabled": False}, "disable-pending", current["version"])
    await drain(svc)
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in tg.calls)


@pytest.mark.parametrize("change", [{"ai_ad_limit": 4}, {"ai_ad_penalty": "delete"}])
async def test_pending_ai_penalty_rechecks_saved_limit_and_disposition(env, change):
    await settings(env, ai_ad_limit=2)
    for identifier in (1, 2):
        await env[3].message(message(identifier, f"任意审核结果{identifier}"))
    async with PolicyExecution(env[1]) as session:
        group = await session.get(Group, -1001)
        for identifier in (1, 2):
            await record_ai_advertisement(session, group, 42, identifier, f"任意审核结果{identifier}", "")
        assert await session.scalar(select(Action).where(Action.kind == "ai_ad_mute"))
    await settings(env, **change)
    await drain(env[3])
    assert not any(method == "restrict_chat_member" for method, _ in env[2].calls)
    assert [data["message_id"] for method, data in env[2].calls if method == "delete_message"] == [1, 2]


async def test_ai_window_and_threshold_are_read_from_saved_settings(env, monkeypatch):
    from purrai import ai_advertising
    stamp = [now()]
    monkeypatch.setattr(ai_advertising, "now", lambda: stamp[0])
    await settings(env, ai_ad_window_hours=2, ai_ad_limit=2)
    async with PolicyExecution(env[1]) as session:
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 1, "任意审核甲", "成员")
    stamp[0] += 3601
    await settings(env, ai_ad_window_hours=1)
    async with env[1].sessions() as session:
        assert await advertisement_count(session, -1001, 42) == 0
    await settings(env, ai_ad_window_hours=2)
    async with PolicyExecution(env[1]) as session:
        assert await advertisement_count(session, -1001, 42) == 1
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 2, "任意审核乙", "成员")
    await drain(env[3])
    assert any(method == "restrict_chat_member" for method, _ in env[2].calls)


@pytest.mark.parametrize("change", [{"ai_ad_limit": 4}, {"ai_ad_penalty": "delete"}])
async def test_ai_policy_is_rechecked_after_slow_telegram_lookup(env, monkeypatch, change):
    await settings(env, ai_ad_limit=2)
    async with PolicyExecution(env[1]) as session:
        group = await session.get(Group, -1001)
        for identifier in (1, 2):
            await record_ai_advertisement(session, group, 42, identifier, "任意审核正文", "成员")
    call = env[2].call
    changed = False

    async def slow(method, **kwargs):
        nonlocal changed
        result = await call(method, **kwargs)
        if method == "get_chat" and not changed:
            changed = True
            await settings(env, **change)
        return result

    monkeypatch.setattr(env[2], "call", slow)
    await drain(env[3])
    assert changed
    assert not any(method == "restrict_chat_member" for method, _ in env[2].calls)


async def test_notice_setting_applies_to_group_receipts_and_not_private_receipts(env):
    await settings(env, notice_seconds=45)
    async with env[1].transaction() as session:
        for kind, destination in [("warn", -1001), ("admin_notify", -1001), ("admin_notify", 1)]:
            await enqueue(session, kind, f"notice-{kind}-{destination}", -1001,
                          payload={"chat_id": destination, "action_ids": [], "text": "任意提醒"})
    await drain(env[3])
    async with env[1].sessions() as session:
        cleanups = (await session.scalars(select(Action).where(Action.dedupe_key.startswith("cleanup:notice:")))).all()
        assert len(cleanups) == 2
        for item in cleanups:
            original = await session.get(Action, item.dedupe_key.removeprefix("cleanup:notice:"))
            assert 44 <= item.available_at - original.updated_at <= 46
    assert await NoticeCleanup(env[1]).reconcile() == 0


async def test_spam_duration_and_media_tenure_use_saved_settings(env):
    from purrai.media_gate import restricted_newcomer
    cfg = await settings(env, spam_mute_seconds=600, repeat_count=3, similar_enabled=False, burst_enabled=False,
                         media_newcomer_days=2)
    member = Member(group_id=-1001, user_id=47, joined_at=now() - 3 * 86400, join_observed=True, present=True)
    assert not restricted_newcomer(member, now(), cfg.model_dump())
    cfg = await settings(env, media_newcomer_days=4)
    assert restricted_newcomer(member, now(), cfg.model_dump())
    for identifier in (1, 2, 3):
        await env[3].message(message(identifier, "任意重复内容"))
    await drain(env[3])
    mute = next(data for method, data in env[2].calls if method == "restrict_chat_member")
    assert 595 <= mute["until_date"] - now() <= 605


async def test_command_learning_and_chat_opening_policies_use_saved_values(env, client):
    from purrai.chat_participation import assess, low_information
    cfg = await settings(env, chat_command_min_users=1, chat_command_threshold=3,
                         chat_evidence_hours=1, chat_min_opening_chars=0,
                         chat_ignore_numeric_openings=False, chat_repeat_hours=0)
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    document["recognition"]["candidate_patterns"] = [{"id": "arbitrary", "title": "任意口令", "enabled": True, "pattern": "琥珀口令"}]
    await save(client, document)
    async with PolicyExecution(env[1]) as session:
        rules = await effective_rules(session, -1001)
        for identifier in (10, 11, 12):
            data = await chat_commands.observe(session, -1001, message(identifier, "琥珀口令"), cfg, eligible=True, rules=rules)
        assert chat_commands.learned(data, cfg, rules) == ["琥珀口令"]
        assert not low_information("12", cfg, env[2])
        for identifier in (3, 4):
            assert not await assess(session, -1001, 0, identifier, 42, "同一消息", cfg, env[2], remember=True)
    cfg = await settings(env, chat_command_min_users=2, chat_min_opening_chars=4, chat_ignore_numeric_openings=True, chat_repeat_hours=1)
    assert not chat_commands.learned(data, cfg, rules)
    assert low_information("12345", cfg, env[2])
    assert low_information("三个字", cfg, env[2])
    async with env[1].transaction() as session:
        assert await assess(session, -1001, 0, 5, 42, "同一消息", cfg, env[2])


async def test_ai_criteria_reach_model_and_changes_invalidate_inflight_and_pending_actions(client, env):
    worker, model = await reviewer(env)
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    document["ai_review_instructions"] = "测试准则甲：根据管理员定义进行审核。"
    await save(client, document)
    original = model.completion
    prompts = []

    async def completion(system, payload, **kwargs):
        prompts.append(system)
        return await original(system, payload, **kwargs)

    model.completion = completion
    model.wait = True
    await env[3].message(message(901, "没有本地命中的任意正文"))
    task = asyncio.create_task(worker.run_one())
    await asyncio.wait_for(model.entered.wait(), 5)
    document["ai_review_instructions"] = "测试准则乙：另一份管理员定义。"
    await save(client, document)
    model.release.set()
    await task
    assert "测试准则甲" in prompts[0]
    assert (await records(env[1], AdvertisementReview))[0].state == "pending"
    assert not [row for row in await records(env[1], Action) if row.kind in ("delete", "ai_ad_mute")]
    assert await worker.run_one()
    assert "测试准则乙" in prompts[1] and "测试准则甲" not in prompts[1]
    document["ai_review_instructions"] = "测试准则丙：修改后需要重新判断。"
    await save(client, document)
    await drain(env[3])
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in env[2].calls)


async def test_public_income_solicitation_criteria_are_saved_and_used_by_both_model_paths(client, env):
    import json
    from importlib.resources import files
    from purrai.advertisement_review import prompt
    from purrai.chat_generation import ChatGeneration
    public = json.loads(files("purrai").joinpath("assets", "public_moderation.json").read_text(encoding="utf-8"))
    criteria = public["business_rules"]["ai_review_instructions"]
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    document["ai_review_instructions"] = criteria
    await save(client, document)
    async with env[1].sessions() as session:
        rules = await effective_rules(session, -1001)
        assert criteria in prompt(rules)
        assert criteria in ChatGeneration(None, None).prompt(GroupSettings(), rules=rules)
        assert "没有引流目标时判非广告" in criteria


async def test_old_client_keeps_explicit_cooldown_metadata(client, env):
    current = (await client.get("/api/settings/business-policy")).json()
    entry = next(rule for stage in current["document"]["stages"] for rule in stage["rules"] if rule["advertiser_cooldown"])
    identifier = entry["id"]
    entry.pop("advertiser_cooldown")
    await save(client, current["document"])
    async with env[1].sessions() as session:
        saved = (await session.get(SharedPolicy, 1)).business_rules
    assert next(rule for stage in saved["stages"] for rule in stage["rules"] if rule["id"] == identifier)["advertiser_cooldown"]


async def test_learned_target_uses_selected_action_without_escalating_to_ban(env):
    await settings(env, learned_target_penalty="delete")
    await rule(env, kind="keyword", value="琥珀邀请", penalty="permanent_mute")
    await env[3].message(message(1001, "琥珀邀请，电话13800138000"))
    async with env[1].sessions() as session:
        common = await session.get(SharedPolicy, 1)
        assert common.rules[0]["penalty"] == "delete"
    await drain(env[3])
    await env[3].message(message(1002, "电话13800138000", user_id=43))
    await drain(env[3])
    assert any(method == "delete_message" and data["message_id"] == 1002 for method, data in env[2].calls)
    assert not any(method in ("restrict_chat_member", "ban_chat_member") and data["user_id"] == 43
                   for method, data in env[2].calls)


async def test_chat_compatibility_review_also_uses_saved_criteria(client, env):
    from purrai.chat_model import ChatReply
    chat, model = await configured(env)
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    document["ai_review_instructions"] = "任意聊天审核准则甲"
    await save(client, document)
    original = model.reply

    async def reply(system, payload):
        assert "任意聊天审核准则甲" in system
        document["ai_review_instructions"] = "任意聊天审核准则乙"
        await save(client, document)
        return await original(system, payload)

    model.reply = reply
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    await chat.observe(message(1101, "本次用于模型判断的普通正文"))
    assert await chat.run_one()
    await drain(env[3])
    assert not any(method in ("restrict_chat_member", "ban_chat_member", "delete_message") for method, _ in env[2].calls)
    await chat.close()


@pytest.mark.parametrize('category', ['ban', 'contact', 'spam', 'repeat', 'advertising_stats', 'spam_stats', 'ai_history', 'target_learning', 'media_gate'])
async def test_saved_reason_policy_replaces_disables_clears_and_matches_sql(client, env, category):
    from purrai.reason_policy import ReasonPolicy, matches, sql_matches
    from sqlalchemy import literal
    document = (await client.get('/api/settings/business-policy')).json()['document']
    document['reason_policy'] = ReasonPolicy().model_dump()
    entry = {'id': 'arbitrary-reason', 'title': '任意判定文字', 'enabled': True, 'mode': 'prefix', 'value': '琥珀_%A'}
    document['reason_policy'][category] = [entry]
    for mode in ('prefix', 'exact', 'contains'):
        entry.update(mode=mode, value='琥珀_%A', enabled=True)
        for operation in ('create', 'replace', 'disable', 'clear'):
            if operation == 'replace':
                entry['value'] = '珊瑚_%B'
            elif operation == 'disable':
                entry['enabled'] = False
            elif operation == 'clear':
                document['reason_policy'][category] = []
            await save(client, document)
            async with env[1].sessions() as session:
                rules = await effective_rules(session, -1001)
                for text in ('琥珀_%A', '琥珀_%A尾', '前琥珀_%A', '珊瑚_%B', '珊瑚_%B尾', '琥珀XYA', '琥珀_%a'):
                    expected = operation not in ('disable', 'clear') and (text == entry['value'] if mode == 'exact' else
                        text.startswith(entry['value']) if mode == 'prefix' else entry['value'] in text)
                    assert matches(text, category, rules) == expected
                    assert bool(await session.scalar(select(sql_matches(literal(text), category, rules)))) == expected
            assert (await client.get('/api/settings/business-policy')).json()['document']['reason_policy'] == document['reason_policy']
        document['reason_policy'][category] = [entry]


async def test_reason_policy_drives_actual_dashboard_and_history_import(client, env):
    from purrai.models import Violation
    document = (await client.get('/api/settings/business-policy')).json()['document']
    for category in ('advertising_stats', 'ai_history'):
        document['reason_policy'][category] = [{'id': category, 'title': '任意分类', 'enabled': True, 'mode': 'prefix', 'value': '珊瑚判定'}]
    async with env[1].transaction() as session:
        session.add(Violation(group_id=-1001, message_id=991, user_id=42, excerpt='完整的历史样本文字', reasons=['珊瑚判定：详情']))
    for enabled in (True, False):
        for category in ('advertising_stats', 'ai_history'):
            document['reason_policy'][category][0]['enabled'] = enabled
        await save(client, document)
        result = await client.get('/api/dashboard')
        assert result.status_code == 200 and result.json()['advertising_today'] == int(enabled)
        result = await client.post('/api/groups/-1001/advertisement-samples/import-history', json={'apply': False}, headers={'Idempotency-Key': f'reason-import-{enabled}'})
        assert result.status_code == 200, result.text
        assert result.json()['count'] == int(enabled)


async def test_saved_chat_expiry_controls_observation_queue_and_send_recheck(env):
    from purrai.models import ChatMessage, ChatTurn
    chat, model = await configured(env)
    await settings(env, chat_message_max_age_seconds=300)
    await chat.observe(message(960, '柴郡喵帮忙想想周末去哪里走走', date=int(now()) - 240))
    assert await chat.run_one() and len(model.calls) == 1
    await chat.observe(message(961, '柴郡喵帮忙想想下周去哪里散步', date=int(now()) - 240))
    await settings(env, chat_message_max_age_seconds=30)
    assert not await chat.run_one() and len(model.calls) == 1
    await chat.observe(message(962, '柴郡喵帮忙想想明天去哪里露营', date=int(now()) - 60))
    assert not await chat.run_one() and len(model.calls) == 1
    model.release = asyncio.Event()
    model.started.clear()
    await chat.observe(message(963, '柴郡喵帮忙想想今晚去哪里吃饭'))
    task = asyncio.create_task(chat.run_one())
    await asyncio.wait_for(model.started.wait(), 5)
    async with env[1].transaction() as session:
        target = await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 963))
        target.sent_at = now() - 15
    await settings(env, chat_message_max_age_seconds=10)
    sent = len([1 for method, _ in env[2].calls if method == 'send_message'])
    model.release.set()
    assert await task
    assert len([1 for method, _ in env[2].calls if method == 'send_message']) == sent
    async with env[1].sessions() as session:
        turn = await session.scalar(select(ChatTurn).where(ChatTurn.message_id == 963))
        assert turn.state == 'skipped'
    await chat.close()


async def test_saved_chat_windows_affect_cached_context_dialogue_and_bursts(env):
    from purrai.chat_context import ChatContext
    from purrai.chat_participation import has_dialogue, reply_ids
    from purrai.chat_state import message_bursts
    from purrai.models import ChatMessage, ChatTurn
    chat, _ = await configured(env)
    stamp = now()
    async with env[1].transaction() as session:
        session.add(ChatMessage(group_id=-1001, message_id=971, topic_id=0, sender_id=42, sender_name='成员',
            text='原先上下文里的长消息', sent_at=stamp-7200, received_at=stamp-7200, eligible=True))
        session.add(ChatTurn(group_id=-1001, message_id=971, topic_id=0, created_at=stamp-7200, sent_message_id=1971, state='sent'))
    context = [{'id': 971, 'speaker_id': 42, 'name': '成员', 'text': '原先上下文里的长消息', 'bot': False, 'sent_at': stamp-7200}]
    for hours, expected in [(3, True), (1, False)]:
        cfg = await settings(env, chat_context_hours=hours)
        async with env[1].sessions() as session:
            assert bool(await ChatContext(env[2]).filter(session, -1001, 0, context, cfg)) is expected
            assert bool(await reply_ids(session, -1001, 0, cfg)) is expected
    for seconds, expected in [(300, True), (30, False), (0, False)]:
        cfg = await settings(env, chat_dialogue_seconds=seconds)
        assert has_dialogue([(42, 971, stamp-60)], 42, 972, stamp, cfg) is expected
    rows = [ChatMessage(topic_id=0, message_id=i+1, sender_id=42, sent_at=stamp+i*20, is_bot=False) for i in range(2)]
    for seconds, count in [(30, 1), (10, 2)]:
        cfg = await settings(env, chat_burst_seconds=seconds)
        assert len(message_bursts(rows, cfg.chat_burst_seconds)) == count
    await chat.close()


async def test_identical_descriptions_keep_stronger_explicit_disposition(client, env):
    from purrai.models import Violation
    document = (await client.get('/api/settings/business-policy')).json()['document']
    for stage in document['stages']:
        stage['rules'] = []
    stage = next(stage for stage in document['stages'] if stage['id'] == 'advertising')
    stage['first_match'] = False
    for action in ('ban', 'delete'):
        entry = arbitrary_rule('advertising', word='琥珀测试正文', action=action)
        entry.update(id='same-label-' + action, reason='相同的说明文字')
        stage['rules'].append(entry)
    await save(client, document)
    await env[3].message(message(981, '琥珀测试正文'))
    await drain(env[3])
    assert any(method == 'ban_chat_member' for method, _ in env[2].calls)
    async with env[1].sessions() as session:
        violation = await session.scalar(select(Violation).where(Violation.message_id == 981))
        assert violation.reason_details['相同的说明文字']['action'] == 'ban'


async def test_queued_legacy_ban_rechecks_disabled_reason_classification(client, env):
    from purrai.models import Violation
    from purrai.moderation_penalties import apply_penalty
    document = (await client.get('/api/settings/business-policy')).json()['document']
    document['reason_policy']['ban'] = [{'id': 'old-ban', 'title': '旧处置', 'enabled': True, 'mode': 'prefix', 'value': '琥珀旧判定'}]
    await save(client, document)
    async with env[1].transaction() as session:
        group = await session.get(Group, -1001)
        member = Member(group_id=-1001, user_id=42, joined_at=0)
        violation = Violation(group_id=-1001, user_id=42, message_id=982, reasons=['琥珀旧判定：详情'], reason_details={})
        session.add_all([member, violation])
        await session.flush()
        await apply_penalty(session, group, violation, member)
        assert await session.scalar(select(Action).where(Action.kind == 'advertisement_ban'))
    document['reason_policy']['ban'][0]['enabled'] = False
    await save(client, document)
    await drain(env[3])
    assert not any(method == 'ban_chat_member' for method, _ in env[2].calls)
    async with env[1].sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == 'advertisement_ban'))).state == 'cancelled'
