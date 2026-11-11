import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, Group, Member, Whitelist, Violation, now
from purrai.rules import assess
from purrai.schemas import GroupSettings
from purrai.shared_policy import effective_rules
from purrai.solicitation_rules import contact_should_block, profile_solicitation_reasons, solicitation_reasons

AD_MESSAGES = [
    "零风险搞老外，能熬夜的来",
    "0 风 险 搞 老 外，能熬夜的来",
    "微信支付宝能钱的来一天10000 @ysppay",
    "招代收 日入7000",
    "五大联赛足球红单.串子推荐🌈天天收米.日赚1万 @xiaoxin1110bot",
    "抖音长期代刷礼物，新老号均可",
    "抖音長期代刷禮物，新老號均可",
    "走私苹果全系列低价出",
    "走私蘋果全系列低價出",
    "各类水果机全系列6折起，一台利润3000，大押担保，安全极速结算！",
    "不是广告，招代收日入7000",
]

NORMAL_MESSAGES = [
    "苹果手机降到6折了，买哪个型号比较好？",
    "我准备卖掉自用 iPhone 13，大家觉得多少钱合适？",
    "这批走私苹果手机案件判了几年？",
    "足球五大联赛这个周末有什么比赛？",
    "所谓足球红单日赚1万是真的吗？",
    "抖音礼物退款怎么处理？",
    "抖音代刷礼物是什么意思？",
    "不要参与代收、跑分和洗钱。",
    "举报：微信支付宝能钱的来一天10000 @ysppay",
    "有人发零风险搞老外，能熬夜的来，这是什么骗局？",
    "“零风险搞老外，能熬夜的来”",
    "“抖音长期代刷礼物，新老号均可”，这是典型广告。",
    "> 走私苹果全系列低价出\n大家别信这些广告",
    "新人可代是什么意思？",
    "个人可三通是什么暗语？",
    "有人发个人可三通，请管理员看看",
    "我想兼职做跨境客服，能熬夜，求建议",
    "我用微信和支付宝一天收款10000，怎么记账？",
    "请教一下三通接头怎么安装？",
    "普通链接 https://x.com/author/status/123 和 @normal_user 都能交流",
]


@pytest.mark.parametrize("text", AD_MESSAGES)
def test_screenshot_advertisements_and_obfuscated_variants_are_recognized(text):
    assert solicitation_reasons(text)


@pytest.mark.parametrize("text", NORMAL_MESSAGES)
def test_normal_discussion_questions_reports_and_quotations_are_not_advertisements(text):
    cfg = GroupSettings(newcomer_links=False, newcomer_mentions=False, newcomer_wallets=False).model_dump()
    rules = [{"kind": "keyword", "value": "抖音代刷礼物", "mode": "compact", "scope": "message", "penalty": "permanent_mute"}]
    result = assess(text, [], rules, cfg)
    assert not result["reasons"] and not result["ban_reasons"] and not result["mute_reasons"]


@pytest.mark.parametrize("name", ["5免.税手.机.6.折.起u Joy", "7免税手机6折起i Yadav", "a抖音1b代2刷"])
def test_marketing_profiles_require_specific_business_combinations(name):
    assert profile_solicitation_reasons(name)


@pytest.mark.parametrize("name", ["奥特曼", "晨~溪/代*發 ★", "手机摄影爱好者", "假币鉴别科普", "抖音代刷骗局揭秘"])
def test_normal_and_anti_fraud_profile_names_are_not_punished(name):
    assert not profile_solicitation_reasons(name)


@pytest.mark.parametrize("name", ["假钞交流群🔥玩妹嫖娼专用", "假币洗钱交流群🔥快递面交都可",
                                 "新~款18最~低~七~折 看~我~介~绍", "假钞🔥嫖娼玩妹赌博日常用"])
def test_all_screenshot_contact_card_names_are_detected_without_avatar_ocr(name):
    assert contact_should_block({"contact": {"first_name": name}}, {"contact_ads_only": True})


async def configure(env):
    _, db, _, _ = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "contact_ads_only": True, "newcomer_links": False,
                          "newcomer_mentions": False, "newcomer_wallets": False, "media_min_text_messages": 0}
        session.add(Member(group_id=-1001, user_id=42, joined_at=now()-30*86400, passed_at=now()-30*86400))


async def test_normal_contact_card_is_allowed_and_is_not_added_to_global_blacklist(env):
    await configure(env)
    _, db, tg, service = env
    await service.message(message(70, "", contact={"first_name": "张三", "phone_number": "+8613900012345", "user_id": 100}))
    await drain(service)
    assert not any(name in ("delete_message", "restrict_chat_member", "ban_chat_member") for name, _ in tg.calls)
    async with db.sessions() as session:
        assert not any(rule["kind"] == "block_target" for rule in await effective_rules(session, -1002))


async def test_advertising_contact_card_is_deleted_banned_and_its_destination_is_learned(env):
    await configure(env)
    _, db, tg, service = env
    await service.message(message(71, "", contact={"first_name": "假币洗钱交流群快递面交都可", "phone_number": "+84522337459", "user_id": 100}))
    await drain(service)
    assert any(name == "delete_message" for name, _ in tg.calls)
    assert any(name == "ban_chat_member" for name, _ in tg.calls)
    async with db.sessions() as session:
        rules = await effective_rules(session, -1002)
        assert any(rule["kind"] == "block_target" and rule["value"] == "phone:84522337459" for rule in rules)
        action = await session.scalar(select(Action).where(Action.kind == "contact_ban"))
        assert action.payload["contact_evidence"]["message"]["contact"]["first_name"].startswith("假币")


@pytest.mark.parametrize("text", ["新.人.可.代", "个人可三通"])
async def test_ambiguous_short_solicitations_only_delete_without_permanent_penalties(env, text):
    await configure(env)
    _, _, tg, service = env
    await service.message(message(72, text))
    await drain(service)
    assert any(name == "delete_message" for name, _ in tg.calls)
    assert not any(name in ("restrict_chat_member", "ban_chat_member") for name, _ in tg.calls)


def test_report_disclaimer_does_not_exempt_a_new_direct_solicitation():
    result = assess("举报骗子，私聊我免费带单领取策略", [], [], GroupSettings().model_dump())
    assert result["ban_reasons"]


@pytest.mark.parametrize("name,blocked", [("张三", False), ("假币洗钱交流群快递面交都可", True)])
async def test_contact_preview_matches_the_live_contact_policy(env, name, blocked):
    from purrai.management import Management
    from purrai.schemas import PreviewInput
    await configure(env)
    settings, db, tg, _ = env
    result = await Management(db, tg, settings=settings).preview(1, -1001, PreviewInput(text="", contact=True, contact_name=name))
    assert result["delete_message"] is blocked
    assert result["ban_member"] is blocked


async def test_old_queued_contact_penalty_requires_advertising_evidence(env):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from purrai.automatic_actions import AutomaticActions
    await configure(env)
    _, db, _, _ = env
    restriction = SimpleNamespace(permanent_ban=AsyncMock())
    action = SimpleNamespace(state="pending", error="", payload={})
    async with db.sessions() as session:
        group = await session.get(Group, -1001)
        member = await session.get(Member, (-1001, 42))
        await AutomaticActions(restriction).contact_ban(session, action, group, None, member)
    assert action.state == "cancelled"
    restriction.permanent_ban.assert_not_awaited()


async def test_marketing_profile_is_deleted_and_muted_without_banning_the_member(env):
    await configure(env)
    _, _, tg, service = env
    incoming = message(73, "大家好")
    incoming["from"]["first_name"] = "a抖音1b代2刷"
    await service.message(incoming)
    await drain(service)
    assert any(name == "delete_message" for name, _ in tg.calls)
    assert any(name == "restrict_chat_member" for name, _ in tg.calls)
    assert not any(name == "ban_chat_member" for name, _ in tg.calls)


def configurable_keywords(words, *, scope='either', penalty='permanent_mute'):
    return [{'kind': 'keyword', 'value': word, 'mode': 'pinyin', 'scope': scope,
             'penalty': penalty, 'enabled': True} for word in words]


async def save_keywords(client, words):
    baseline = (await client.get('/api/settings/defaults')).json()
    response = await client.patch('/api/settings/defaults', json={
        'rules': configurable_keywords(words), 'baseline': baseline, 'request_id': 'configured-keyword-data'})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("name,text", [
    ("六合内幕xu一手", "今天又收米了"),
    ("六·合 内·幕 x_u 一手", "今天又收·米了"),
    ("六 合 內 幕 一 手", "今天又收米了"),
    ("六1合A内B幕一手", "今天又收1米了"),
    ('“六合内幕xu一手”', "今天又收米了"),
    ("六合一手", "内幕消息，今天又收米了"),
    ("内幕一手", "六合今天又收米了"),
])
def test_configured_keywords_cover_profile_body_and_obfuscation(name, text):
    rules = configurable_keywords(['六合', '内幕', '收米'])
    result = assess(text, [], rules, GroupSettings().model_dump(), sender_name=name)
    assert result['name_reasons'] or result['mute_reasons']
    assert not result['ban_reasons']  # The engine follows the configured penalty.


def test_sample_keywords_have_no_hidden_builtin_matching():
    result = assess('今天又收米了', [], [], GroupSettings().model_dump(), sender_name='六合内幕xu一手')
    assert not result['name_reasons'] and not result['reasons'] and not result['ban_reasons']


async def test_keyword_data_added_through_management_applies_to_older_member(env, client):
    await configure(env)
    await save_keywords(client, ['六合', '内幕', '收米'])
    _, db, tg, service = env
    incoming = message(74, '今天又收米了')
    incoming['from'].update(first_name='六合', last_name='内幕xu一手')
    await service.message(incoming, update_id=100)
    await service.message(incoming, update_id=100)
    await drain(service)
    assert [data['message_id'] for method, data in tg.calls if method == 'delete_message'] == [74]
    assert not any(method == 'ban_chat_member' for method, _ in tg.calls)
    assert sum(method == 'restrict_chat_member' for method, _ in tg.calls) == 1
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
        assert '六合' in violation.sender_name and '内幕' in violation.sender_name
        assert any('收米' in reason for reason in violation.reasons)
        assert (await session.scalar(select(Action).where(Action.kind == 'profile_mute'))).state == 'succeeded'


async def test_configured_keyword_profile_added_by_message_edit_is_rechecked(env, client):
    await configure(env)
    await save_keywords(client, ['六合', '内幕', '收米'])
    _, _, tg, service = env
    original = message(75, '1')
    await service.message(original, update_id=100)
    await drain(service)
    assert not any(method in ('delete_message', 'ban_chat_member') for method, _ in tg.calls)
    edited = {**original, 'text': '今天又收米了', 'edit_date': original['date'] + 600,
              'from': {**original['from'], 'first_name': '六合內幕xu一手'}}
    await service.message(edited, edited=True, update_id=101)
    await drain(service)
    assert tg.members[(-1001, 42)]['status'] == 'restricted'
    assert not tg.members[(-1001, 42)]['can_send_messages']


@pytest.mark.parametrize('exemption', ['administrator', 'whitelist', 'disable_rules'])
async def test_configured_keywords_keep_exemptions_and_recheck_enabled_state(env, client, exemption):
    await configure(env)
    policy = await save_keywords(client, ['六合', '内幕', '收米'])
    _, db, tg, service = env
    if exemption == 'administrator':
        tg.members[(-1001, 42)] = {'status': 'administrator'}
    elif exemption == 'whitelist':
        async with db.transaction() as session:
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
    incoming = message(76, '今天又收米了')
    incoming['from']['first_name'] = '六合内幕xu一手'
    await service.message(incoming)
    if exemption == 'disable_rules':
        changed = await client.patch('/api/settings/defaults', json={
            'rules': [{**rule, 'enabled': False} for rule in policy['rules']], 'baseline': policy,
            'request_id': 'disable-keywords-before-execution'})
        assert changed.status_code == 200
    await drain(service)
    assert not any(method in ('ban_chat_member', 'restrict_chat_member') for method, _ in tg.calls)


async def test_arbitrary_keyword_data_can_be_added_changed_and_disabled_without_code(env, client):
    await configure(env)
    policy = await save_keywords(client, ['纸飞机'])
    url = '/api/groups/-1001/rules/preview'
    sample = {'sender_name': '普通成员', 'text': '纸飞机推广'}
    assert (await client.post(url, json=sample)).json()['mute_member']
    changed = await client.patch('/api/settings/defaults', json={
        'rules': [{**policy['rules'][0], 'value': '南瓜'}], 'baseline': policy, 'request_id': 'replace-keyword-data'})
    assert changed.status_code == 200
    assert not (await client.post(url, json=sample)).json()['delete_message']
    assert (await client.post(url, json={**sample, 'text': '南瓜推广'})).json()['mute_member']
    policy = changed.json()
    disabled = await client.patch('/api/settings/defaults', json={
        'rules': [{**policy['rules'][0], 'enabled': False}], 'baseline': policy, 'request_id': 'disable-keyword-data'})
    assert disabled.status_code == 200
    assert not (await client.post(url, json={**sample, 'text': '南瓜推广'})).json()['delete_message']


@pytest.mark.parametrize('penalty', ['delete', 'permanent_mute'])
async def test_keyword_body_penalty_is_configuration_data(env, client, penalty):
    await configure(env)
    baseline = (await client.get('/api/settings/defaults')).json()
    result = await client.patch('/api/settings/defaults', json={
        'rules': configurable_keywords(['收米'], scope='message', penalty=penalty),
        'baseline': baseline, 'request_id': 'choose-keyword-penalty'})
    assert result.status_code == 200
    _, _, tg, service = env
    await service.message(message(77, '今天又收米了'))
    await drain(service)
    assert any(method == 'delete_message' for method, _ in tg.calls)
    assert any(method == 'restrict_chat_member' for method, _ in tg.calls) == (penalty == 'permanent_mute')
