import asyncio
import json

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import GetChat
from sqlalchemy import select

from conftest import drain, join, message
from purrai import sender_profile
from purrai.models import Action, AdvertisementPolicy, AdvertisementReview, AdvertisementSample, Group, Member, Rule, Verification, now
from purrai.rules import assess
from purrai.shared_policy import effective_rules
from test_advertisement_review import records


def profiles(tg, values):
    original = tg.call

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_chat" and kwargs["chat_id"] > 0:
            value = values.get(kwargs["chat_id"], "")
            if isinstance(value, Exception):
                raise value
            return {"id": kwargs["chat_id"], "type": "private", "bio": value}
        return result

    tg.call = call


async def test_join_does_not_read_biography_or_interfere_with_verification(env):
    _, db, tg, service = env
    profiles(tg, {42: "可替换的资料词"})
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="可替换的资料词", scope="bio", penalty="permanent_mute"))
    await service.member_event(join())
    await drain(service)
    verification, = await records(db, Verification)
    assert verification.state == "pending" and verification.message_id
    assert not any(method == "get_chat" and data.get("chat_id") == 42 for method, data in tg.calls)
    assert not [row for row in await records(db, Action) if row.kind == "profile_mute"]


@pytest.mark.parametrize("still_matches", [False, True])
async def test_edit_retains_only_revalidated_biography_evidence(env, still_matches):
    _, db, tg, service = env
    values = {42: "任意简介词"}
    profiles(tg, values)
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value=values[42], scope="bio", penalty="permanent_mute"))
        session.add(Member(group_id=-1001, user_id=42, joined_at=now() - 86400, present=True))
    original = message(18, "普通正文")
    edit = {**original, "text": "新的普通正文", "edit_date": original["date"] + 1}
    await service.message(original, update_id=1)
    await service.message(edit, edited=True, update_id=2)
    if not still_matches:
        values[42] = ""
    await drain(service)
    actions = {row.kind: row.state for row in await records(db, Action)}
    assert actions["delete"] == actions["profile_mute"] == ("succeeded" if still_matches else "cancelled")


class BioModel:
    def __init__(self):
        self.calls = []

    async def completion(self, system, payload, **kwargs):
        rows = json.loads(payload)["messages"]
        self.calls.append(rows)
        return {"decisions": [{"id": row["id"], "advertisement": bool(row.get("bio")) and row["text"].startswith("来我主页"),
                               "scope": "context" if row.get("bio") else "text", "reason": "测试独立资料判定"}
                              for row in rows]}, 100

    async def close(self):
        pass


async def setup(env, values):
    settings, db, tg, service = env
    settings.chat_api_key = "offline-key"
    profiles(tg, values)
    model = BioModel()
    service.advertisement_review.model = model
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "advertising_ban": False, "block_invites": False}
    return service.advertisement_review, model


async def test_bio_fetch_coalesces_and_persists_unavailable_separately_from_empty(env, monkeypatch):
    _, db, tg, _ = env
    values = {42: "琥珀简介", 43: TelegramBadRequest(method=GetChat(chat_id=43), message="chat not found")}
    profiles(tg, values)
    reader = sender_profile.SenderProfiles(db, tg)
    result = await asyncio.gather(*(reader.fetch(42) for _ in range(4)))
    assert all(row["bio"] == "琥珀简介" and row["status"] == "available" for row in result)
    assert len(tg.calls) == 1
    failed = await reader.fetch(43)
    assert failed["status"] == "unavailable" and failed["retry_at"] > now()
    before = len(tg.calls)
    assert (await sender_profile.SenderProfiles(db, tg).fetch(43))["status"] == "unavailable"
    assert len(tg.calls) == before
    monkeypatch.setattr(sender_profile, "now", lambda: failed["retry_at"] + 1)
    values[43] = ""
    assert (await reader.fetch(43))["status"] == "empty"
    quoted = message(1, "普通正文", reply_to_message={"from": {"id": 42}, "bio": "琥珀简介"})
    quoted["_sender_profile"] = {"user_id": 99, "status": "available", "bio": "其他用户的简介"}
    assert sender_profile.biography(quoted) == ""
    quoted["sender_chat"] = {"id": -1009}
    quoted["_sender_profile"]["user_id"] = 42
    assert sender_profile.biography(quoted) == ""


async def test_bio_keyword_saved_replaced_and_disabled_through_admin_api(env, client):
    _, db, _, _ = env
    response = await client.post('/api/groups/-1001/rules', headers={'Idempotency-Key': 'bio-rule-create'}, json={
        'kind': 'keyword', 'value': '琥珀星河', 'scope': 'bio', 'penalty': 'permanent_mute',
        'request_id': 'bio-rule-create'})
    assert response.status_code == 200, response.text
    rule = response.json()

    async def verdict(value):
        async with db.sessions() as session:
            group = await session.get(Group, -1001)
            return assess('普通聊天', [], await effective_rules(session, -1001), group.settings, sender_bio=value)

    assert (await verdict('琥珀星河'))['bio_reasons']
    preview = await client.post('/api/groups/-1001/rules/preview', json={'text': '普通聊天', 'sender_bio': '琥珀星河'})
    assert preview.status_code == 200 and preview.json()['mute_member']
    updated = {**{key: rule[key] for key in ('kind', 'value', 'mode', 'scope', 'context_value', 'meaning', 'penalty', 'name_penalty', 'enabled')},
               'value': '珊瑚远航', 'request_id': 'bio-rule-edit'}
    response = await client.put('/api/groups/-1001/rules/' + rule['id'], headers={'If-Match': str(rule['version']), 'Idempotency-Key': 'bio-rule-edit'}, json=updated)
    assert response.status_code == 200, response.text
    rule = response.json()
    assert not (await verdict('琥珀星河'))['bio_reasons']
    assert (await verdict('珊瑚远航'))['bio_reasons']
    response = await client.put('/api/groups/-1001/rules/' + rule['id'], headers={'If-Match': str(rule['version']), 'Idempotency-Key': 'bio-rule-disable'},
                               json={**updated, 'enabled': False, 'request_id': 'bio-rule-disable'})
    assert response.status_code == 200, response.text
    assert not (await verdict('珊瑚远航'))['bio_reasons']


@pytest.mark.parametrize('penalty,method', [('delete', 'delete_message'), ('permanent_mute', 'restrict_chat_member'), ('ban', 'ban_chat_member')])
async def test_native_bio_rule_uses_configured_action_and_rechecks_current_profile(env, penalty, method):
    _, db, tg, service = env
    values = {42: '琥珀星河', 43: '琥珀星河'}
    profiles(tg, values)
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind='keyword', value='琥珀星河', scope='bio', penalty=penalty))
        for uid in (42, 43):
            session.add(Member(group_id=-1001, user_id=uid, joined_at=now() - 86400 * 30, present=True))
    await service.message(message(1, '正常正文'))
    await service.message(message(2, '正常正文', user_id=43))
    values[43] = '已改成普通简介'
    await drain(service)
    effects = [kw for name, kw in tg.calls if name == method]
    assert effects
    if method == 'delete_message':
        assert [kw['message_id'] for kw in effects] == [1]
    else:
        assert [kw['user_id'] for kw in effects] == [42]


async def test_profile_rate_limit_is_shared_and_persisted_without_blocking_body_calls(env):
    _, db, tg, _ = env
    profiles(tg, {42: TelegramRetryAfter(method=GetChat(chat_id=42), message='wait', retry_after=120), 43: '普通简介'})
    reader = sender_profile.SenderProfiles(db, tg)
    first = await reader.fetch(42)
    before = len(tg.calls)
    other = await sender_profile.SenderProfiles(db, tg).fetch(43)
    assert other['status'] == 'unavailable' and other['error'] == 'rate_limited'
    assert other['retry_at'] >= first['retry_at'] - 1 and len(tg.calls) == before
    await tg.call('get_updates')
    assert len(tg.calls) == before + 1


async def test_bio_samples_are_managed_separately_from_body_samples(client):
    path = '/api/groups/-1001/advertisement-samples'
    text = '任意可替换的资料样本用于简介管理验证'
    data = {'text': text, 'scope': 'bio', 'reason': '管理员确认资料广告'}
    response = await client.post(path, json=data, headers={'Idempotency-Key': 'bio-sample-create'})
    assert response.status_code == 200, response.text
    row = response.json()
    assert (await client.post(path + '/preview', json={'text': text, 'scope': 'text'})).json()['action'] == 'review'
    assert (await client.post(path + '/preview', json={'text': text, 'scope': 'bio'})).json()['action'] == 'review'
    changed = '另一份完全不同的用户简介文字也能参与匹配'
    response = await client.put(path + '/' + row['id'], json={**data, 'text': changed},
        headers={'Idempotency-Key': 'bio-sample-change', 'If-Match': str(row['version'])})
    assert response.status_code == 200, response.text
    row = response.json()
    assert (await client.post(path + '/preview', json={'text': text, 'scope': 'bio'})).json()['action'] == 'review'
    assert (await client.post(path + '/preview', json={'text': changed, 'scope': 'bio'})).json()['action'] == 'review'
    response = await client.put(path + '/' + row['id'], json={**data, 'text': changed, 'enabled': False},
        headers={'Idempotency-Key': 'bio-sample-disable', 'If-Match': str(row['version'])})
    assert response.status_code == 200, response.text
    assert (await client.post(path + '/preview', json={'text': changed, 'scope': 'bio'})).json()['action'] == 'review'


async def test_bio_is_context_for_current_message_and_cannot_become_a_standalone_sample(env):
    bio = "今年\u200c最新风口/一个月买车/三个月买房/加入我们一起参与"
    values = {42: bio, 43: bio}
    worker, model = await setup(env, values)
    service = env[3]
    await service.message(message(1, "今天才搞90\u20600，兄弟们今天多少"))
    await service.message(message(2, "其他人的普通消息", user_id=44))
    await service.message(message(3, "来我主页领取活动资格"))
    assert await worker.run_one()
    request = next(row for row in model.calls[0] if row['text'] == '来我主页领取活动资格')
    assert request['bio'] == '今年最新风口/一个月买车/三个月买房/加入我们一起参与'
    assert [item['message_id'] for item in request['recent_messages']] == [1]
    samples = await records(env[1], AdvertisementSample)
    assert len(samples) == 1 and samples[0].scope == 'context'
    assert len(model.calls) == 1
    await service.message(message(4, "换一个短句", user_id=43))
    reviews = await records(env[1], AdvertisementReview)
    assert next(row for row in reviews if row.message_id == 4).state == 'pending'
    assert await worker.run_one()
    await drain(service)
    assert {kw['message_id'] for method, kw in env[2].calls if method == 'delete_message'} == {3}


async def test_changed_bio_cancels_queued_model_penalty(env):
    values = {42: "琥珀星河活动招募欢迎加入我们领取资格 https://example.test/a"}
    worker, _ = await setup(env, values)
    await env[3].message(message(1, "来我主页领取活动资格"))
    assert await worker.run_one()
    values[42] = "现在只记录日常生活"
    await drain(env[3])
    assert not any(method == 'delete_message' for method, _ in env[2].calls)
    assert all(row.state == 'cancelled' for row in await records(env[1], Action) if row.kind == 'delete')


@pytest.mark.parametrize("quote", [None, "", 42, "用自己的话说明产品推广行为"])
async def test_ai_advertisement_decision_applies_without_matching_its_explanation_to_original(env, quote):
    worker, model = await setup(env, {42: "青岚软件，产品咨询 https://example.test/contact"})
    original = model.completion

    async def unsupported(*args, **kwargs):
        result, tokens = await original(*args, **kwargs)
        decision = result["decisions"][0]
        decision["advertisement"] = True
        decision["reason"] = "该账号在群内主动介绍其软件能力，是产品推广"
        if quote is None:
            decision.pop("message_evidence", None)
        else:
            decision["message_evidence"] = quote
        return result, tokens

    model.completion = unsupported
    await env[3].message(message(551, "青岚软件能自动整理表格、生成报告，桌面和手机都能使用"))
    assert await worker.run_one()
    assert len(model.calls) == 1
    review, = await records(env[1], AdvertisementReview)
    assert review.state == "advertisement" and review.result["advertisement"]
    assert review.result["reason"] == "该账号在群内主动介绍其软件能力，是产品推广"
    await drain(env[3])
    assert any(method == "delete_message" and data["message_id"] == 551 for method, data in env[2].calls)
    assert len(await records(env[1], AdvertisementSample)) == 1


async def test_single_review_receives_current_keyword_meanings_profile_and_body(env, client):
    bio = "简介特有词与个人资料，估算丨ooo点 https://example.test/z000"
    worker, model = await setup(env, {42: bio})
    path = "/api/groups/-1001/rules"
    body = {"kind": "keyword", "value": "琥珀星河", "mode": "pinyin", "scope": "either",
            "meaning": "任意替换词的语义说明，命中仍需判断主动招揽", "penalty": "ai_confirmed_mute"}
    response = await client.post(path, json=body, headers={"Idempotency-Key": "recheck-hint-create"})
    assert response.status_code == 200, response.text
    rule = response.json()
    for scope, value in (("name", "昵称特有词"), ("bio", "简介特有词")):
        saved = await client.post(path, json={**body, "value": value, "scope": scope},
                                  headers={"Idempotency-Key": "recheck-hint-" + scope})
        assert saved.status_code == 200, saved.text

    async def check(identifier):
        incoming = message(identifier, "琥.珀\u200b星河与珊瑚远航都是今天讨论的词，记录1️⃣0️⃣0️⃣0️⃣点，版本o2 |2026")
        incoming["from"]["first_name"] = "昵称特有词"
        await env[3].message(incoming)
        before = len(model.calls)
        assert await worker.run_one()
        batch, = model.calls[before:]
        assert batch[0]["name"] == "昵称特有词"
        assert batch[0]["bio"] == bio.replace("，", ",")
        assert batch[0]["number_readings"] == {"text": {"1️⃣0️⃣0️⃣0️⃣": "1000"}, "bio": {"丨ooo": "1000"}}
        assert len(batch[0]["keyword_hints"]) >= 2
        assert next(row for row in await records(env[1], AdvertisementReview) if row.message_id == identifier).state == "clean"
        return [item for item in batch[0]["keyword_hints"] if item["matched_fields"] == ["message"]]

    hints = await check(501)
    assert [(item["keyword"], item["meaning"]) for item in hints] == [(body["value"], body["meaning"])]
    body.update(value="珊瑚远航", meaning="管理员修改后的新含义")
    response = await client.put(path + "/" + rule["id"], json=body,
        headers={"Idempotency-Key": "recheck-hint-edit", "If-Match": str(rule["version"])})
    assert response.status_code == 200, response.text
    rule = response.json()
    hints = await check(502)
    assert [(item["keyword"], item["meaning"]) for item in hints] == [(body["value"], body["meaning"])]
    response = await client.put(path + "/" + rule["id"], json={**body, "enabled": False},
        headers={"Idempotency-Key": "recheck-hint-disable", "If-Match": str(rule["version"])})
    assert response.status_code == 200, response.text
    assert await check(503) == []
    await drain(env[3])
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in env[2].calls)


@pytest.mark.parametrize("bio", ["我的开源项目和交流群 https://example.test", ConnectionError("offline")])
async def test_join_and_telegram_membership_notice_never_queue_bio_reviews(env, bio):
    worker, model = await setup(env, {42: bio})
    await env[3].member_event(join(42))
    await env[3].message(message(120, "", new_chat_members=[{"id": 42, "first_name": "成员"}]))
    await worker.run_one()
    assert not await records(env[1], AdvertisementReview)
    assert not model.calls


async def test_disabling_bio_stops_fetching_and_does_not_skip_body_review(env):
    worker, model = await setup(env, {42: "这段简介不应该被读取"})
    async with env[1].transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, 'bio_advertising_enabled': False}
    await env[3].message(message(1, "只检查正常正文"))
    assert await worker.run_one()
    assert model.calls[0][0]['bio'] == ''
    assert not any(method == 'get_chat' and kwargs.get('chat_id') == 42 for method, kwargs in env[2].calls)


async def test_matching_bio_sample_and_personal_invite_cannot_punish_normal_chat(env):
    bio = "我维护的开源项目和交流群 https://t.me/+synthetic_invite"
    worker, _ = await setup(env, {42: bio})
    async with env[1].transaction() as session:
        session.add(AdvertisementSample(group_id=-1001, source_key="old-bio", text=bio, scope="bio",
                                        reason="历史简介样本", source="manual"))
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "block_invites": True}
    await env[3].message(message(1, "谢谢，我已经修好这个问题了"))
    assert await worker.run_one()
    await drain(env[3])
    assert (await records(env[1], AdvertisementReview))[0].state == "clean"
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in env[2].calls)
    async with env[1].sessions() as session:
        assert not [r for r in await effective_rules(session, -1001) if r["kind"] == "block_target"]


async def test_product_profile_is_sent_with_body_but_only_promotional_body_is_advertising(env):
    bio = "青岚绘图器，联系入口 https://example.test/contact"
    worker, model = await setup(env, {42: bio})
    ordinary = message(651, "今天有人解决这个问题了吗")
    promotion = message(652, "青岚绘图器：自动读入数据、绘制图表，手机和桌面端都能使用")
    for incoming in (ordinary, promotion):
        incoming["from"]["first_name"] = "青岚绘图器"

    async def assess_bodies(system, payload, **kwargs):
        rows = json.loads(payload)["messages"]
        model.calls.append(rows)
        return {"decisions": [{"id": row["id"], "advertisement": row["text"].startswith("青岚绘图器"),
            "scope": "context", "reason": "结合本条正文判断是否主动宣传产品"}
            for row in rows]}, 100

    model.completion = assess_bodies
    await env[3].message(ordinary)
    await env[3].message(promotion)
    assert await worker.run_one()
    for item in model.calls[0]:
        assert item["name"] == "青岚绘图器" and item["bio"] == bio.replace("，", ",") and item["text"]
    reviews = {row.message_id: row for row in await records(env[1], AdvertisementReview)}
    assert reviews[651].state == "clean" and reviews[652].state == "advertisement"
    assert reviews[652].result["reason"] == "结合本条正文判断是否主动宣传产品"
    await drain(env[3])
    assert {args["message_id"] for method, args in env[2].calls if method == "delete_message"} == {652}


async def test_clean_bio_with_personal_link_does_not_create_a_sample_or_blacklist(env):
    worker, model = await setup(env, {42: '个人作品记录 https://ordinary.test'})

    async def clean(system, payload, **kwargs):
        rows = json.loads(payload)['messages']
        return {'decisions': [{'id': row['id'], 'advertisement': False, 'scope': 'bio', 'reason': '普通个人资料'} for row in rows]}, 10

    model.completion = clean
    await env[3].message(message(1, '早上好'))
    assert await worker.run_one()
    assert (await records(env[1], AdvertisementReview))[0].state == 'clean'
    assert not await records(env[1], AdvertisementSample)
    async with env[1].sessions() as session:
        assert not [rule for rule in await effective_rules(session, -1001) if rule['kind'] == 'block_target']


@pytest.mark.parametrize('change', ['profile', 'bio_setting', 'review_setting'])
async def test_bio_penalties_recheck_profile_and_settings_after_network_wait(env, change):
    worker, _ = await setup(env, {42: '琥珀星河资料活动面向所有成员开放欢迎加入一起领取参与资格'})
    async with env[1].transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, 'ai_ad_limit': 1}
    await env[3].message(message(1, '来我主页领取活动资格'))
    assert await worker.run_one()
    assert any(row.kind == 'ai_ad_mute' for row in await records(env[1], Action))
    original = env[2].call
    edited = False

    async def change_during_lookup(method, **kwargs):
        nonlocal edited
        result = await original(method, **kwargs)
        if method == 'get_chat' and kwargs.get('chat_id') == 42:
            if change == 'profile':
                return {**result, 'bio': '改为普通个人资料'}
            if not edited:
                edited = True
                async with env[1].transaction() as session:
                    if change == 'bio_setting':
                        group = await session.get(Group, -1001)
                        group.settings = {**group.settings, 'bio_advertising_enabled': False}
                    else:
                        session.add(AdvertisementPolicy(group_id=-1001, config={'enabled': False}, version=1))
        return result

    env[2].call = change_during_lookup
    await drain(env[3])
    assert not any(method in ('delete_message', 'restrict_chat_member') for method, _ in env[2].calls)
    assert all(row.state == 'cancelled' for row in await records(env[1], Action) if row.kind in ('delete', 'ai_ad_mute'))


async def test_bio_ai_keyword_reads_latest_rule_after_profile_lookup(env):
    worker, model = await setup(env, {42: '琥珀星河资料活动欢迎加入一起领取参与资格'})
    async with env[1].transaction() as session:
        session.add(Rule(id='bio-keyword', group_id=-1001, kind='keyword', value='琥珀星河', scope='bio', penalty='ai_confirmed_mute'))
    await env[3].message(message(1, '来我主页领取活动资格'))
    assert await worker.run_one()
    assert model.calls[0][0]['keyword_hints'][0]['matched_fields'] == ['bio']
    original = env[2].call

    async def disable_during_lookup(method, **kwargs):
        result = await original(method, **kwargs)
        if method == 'get_chat' and kwargs.get('chat_id') == 42:
            async with env[1].transaction() as session:
                (await session.get(Rule, 'bio-keyword')).enabled = False
        return result

    env[2].call = disable_during_lookup
    from purrai.action_execution import ActionExecution
    from purrai.ai_advertising import keyword_penalty_applies
    async with ActionExecution(env[1]) as session:
        session.info['telegram'] = env[2]
        group = await session.get(Group, -1001)
        action = await session.scalar(select(Action).where(Action.kind == 'ai_ad_mute'))
        assert action is not None
        assert not await keyword_penalty_applies(session, action, group)


async def test_bio_target_learning_does_not_reuse_body_link_metadata(env):
    from types import SimpleNamespace
    from purrai.promotion_targets import remember_targets
    bio_message = message(1, '正文 https://unrelated.test')
    bio_message['_sender_profile'] = {'user_id': 42, 'status': 'available', 'bio': '琥珀星河 https://t.me/+synthetic_invite'}
    violation = SimpleNamespace(id='bio-proof', message_id=1, rule_mute=False, reasons=['测试已确认简介广告'])
    async with env[1].transaction() as session:
        group = await session.get(Group, -1001)
        targets = await remember_targets(session, group, violation, {'message': bio_message,
            'bio_dependent': True, 'confirmed_advertisement': True,
            'links': [{'id': -1009009, 'type': 'channel'}]})
        assert targets == ['invite:synthetic_invite']
