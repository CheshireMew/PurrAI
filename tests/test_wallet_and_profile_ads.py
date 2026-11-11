import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.chat_input import input_reasons
from purrai.chat_safety import output_reasons
from purrai.models import Action, Group, Member, Violation, Whitelist, default_settings, now
from purrai.rules import assess
from test_newcomer_policy import ADDRESSES

WALLET_REASON = "发送加密货币或代币合约地址"
WALLET_AD = "蝴蝶登月 首个无限托底回购护盘燃烧金库，社区启动\n\n0x81e1831cd9f20ee3404ade85d1cb242b8d5c7777"
PROFILE_AD = "又賺錢了！我跟著我主頁社區做合約，一周直接翻倉了！都說了跟對他很重要，別等爆倉了才後悔！"
PROFILE_SIMPLE = "又赚钱了！我跟着我主页社区做合约，一周直接翻仓了！都说了跟对他很重要，别等爆仓了才后悔！"


@pytest.mark.parametrize("member_kind", ["unknown", "old", "new"])
@pytest.mark.parametrize("source", ["text", "caption", "edited"])
async def test_wallet_ad_deletes_without_join_record_or_tenure_limit(env, member_kind, source):
    _, db, tg, svc = env
    if member_kind != "unknown":
        age = 40 * 86400 if member_kind == "old" else 60
        async with db.transaction() as session:
            session.add(Member(group_id=-1001, user_id=42, joined_at=now() - age,
                               passed_at=now() - age, observed_messages=30))
    if source == "edited":
        await svc.message(message(1, "正常讨论"))
    incoming = message(1, WALLET_AD)
    if source == "caption":
        incoming = message(1, "", caption=WALLET_AD, document={"file_id": "document"})
    await svc.message(incoming, edited=source == "edited")
    await drain(svc)
    assert [kw["message_id"] for method, kw in tg.calls if method == "delete_message"] == [1]
    assert not any(method in ("ban_chat_member", "restrict_chat_member") for method, _ in tg.calls)
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
        assert violation.reasons == [WALLET_REASON] and not violation.warning_active


@pytest.mark.parametrize("address", ADDRESSES)
def test_wallet_rule_preserves_case_in_chat_input_and_output_checks(address):
    assert WALLET_REASON in input_reasons("地址：" + address, [], default_settings())
    assert WALLET_REASON in output_reasons("地址：" + address, [], default_settings())


async def test_wallet_in_hidden_link_is_blocked_for_unknown_member(env):
    _, _, tg, svc = env
    incoming = message(1, "查看项目", entities=[{"type": "text_link", "offset": 0, "length": 4,
                        "url": "https://explorer.example/address/" + ADDRESSES[0]}])
    await svc.message(incoming)
    await drain(svc)
    assert any(method == "delete_message" for method, _ in tg.calls)


@pytest.mark.parametrize("exemption", ["administrator", "whitelist", "superadmin", "switch_off", "paused"])
async def test_all_member_wallet_rule_keeps_existing_exemptions(env, exemption):
    settings, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        if exemption == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        if exemption == "switch_off":
            group.settings = {**group.settings, "newcomer_wallets": False}
        if exemption == "paused":
            group.enabled = False
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    if exemption == "superadmin":
        settings.superadmin_ids.append(42)
    await svc.message(message(1, WALLET_AD))
    await drain(svc)
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in tg.calls)


@pytest.mark.parametrize("text", [PROFILE_AD, PROFILE_SIMPLE,
    "又赚了！我主页社群跟单做合约，一周翻倍！",
    "点我头像，跟单合约交易，收益翻倍。",
    "跟随我简介里的社区做合约，盈利翻仓。",
    "我主\u200b頁 社 區跟著做合約，這週賺錢翻倉了！",
    "不是广告，我跟着我主页社区做合约，一周翻倍了！",
])
async def test_profile_promotion_deletes_and_bans_unknown_or_old_member(env, text):
    _, db, tg, svc = env
    await svc.message(message(1, text))
    await drain(svc)
    assert tg.members[(-1001, 42)] == {"status": "kicked", "until_date": 0}
    assert len([method for method, _ in tg.calls if method == "ban_chat_member"]) == 1
    assert any(method == "delete_message" and kw["message_id"] == 1 for method, kw in tg.calls)
    async with db.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "advertisement_ban"))
        assert action.state == "succeeded"


@pytest.mark.parametrize("text", [
    "我这周做合约翻仓了，差点又爆仓。",
    "我跟着社区做合约，这周收益不错。",
    "我主页社区有合约风险资料，请先学习风险。",
    "跟对策略很重要，别等爆仓才后悔。",
    "我主页社区在讨论合约，但没有盈利，也没有跟单。",
    "我主页社区有人跟单做合约，收益不好。",
    "如何识别这种我主页社区跟单合约一周翻倍的广告？",
    "举报：又赚钱了！我跟着我主页社区做合约，一周翻仓了！",
    "有人说我跟着我主页社区做合约一周翻仓，这种可信吗？",
    "不要信我主页社区跟单合约一周翻倍这种话术。",
    "“" + PROFILE_AD + "” 这种广告请小心。",
    "> " + PROFILE_AD,
    "這種跟著我主頁社區做合約翻倉的廣告是不是騙局？",
])
async def test_profile_ad_filter_keeps_discussions_reports_and_quotes(env, text):
    _, _, tg, svc = env
    await svc.message(message(1, text))
    await drain(svc)
    assert not any(method in ("delete_message", "ban_chat_member", "restrict_chat_member") for method, _ in tg.calls)


async def test_profile_ad_switch_rechecked_before_ban(env):
    _, db, tg, svc = env
    await svc.message(message(1, PROFILE_AD))
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "advertising_ban": False}
    await drain(svc)
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)
    assert not assess(PROFILE_AD, [], [], {**default_settings(), "advertising_ban": False})["ban_reasons"]


async def test_profile_ad_caption_and_edit_ban_only_once(env):
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, joined_at=now() - 40 * 86400,
                           passed_at=now() - 40 * 86400, observed_messages=30))
    await svc.message(message(1, "正常讨论"), update_id=1)
    incoming = message(1, "", caption=PROFILE_AD, document={"file_id": "document"})
    await svc.message(incoming, edited=True, update_id=2)
    await svc.message(incoming, edited=True, update_id=2)
    await drain(svc)
    assert len([method for method, _ in tg.calls if method == "ban_chat_member"]) == 1
    assert len([method for method, _ in tg.calls if method == "delete_message"]) == 1


def test_profile_promotion_is_also_excluded_from_chat_and_generated_output():
    assert input_reasons(PROFILE_AD, [], default_settings())
    assert output_reasons(PROFILE_AD, [], default_settings())
