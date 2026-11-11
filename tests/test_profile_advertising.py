import pytest
from sqlalchemy import select

from conftest import drain, join, message
from purrai.archive import capture
from purrai.models import Action, Group, Member, Violation, Whitelist, default_settings, now
from purrai.rules import assess
from purrai.schemas import GroupSettings
from purrai.chat_model import ChatReply
from test_chat import configured, age_reply

AD_NAME = "有🫵码来赚🫵简介一天8OOO+"


@pytest.mark.parametrize("name", [AD_NAME, "有码来赚 简介 一天8000+", "有\u200b碼來賺-簡介-一天８ＯＯＯ+", "收款码私聊日入8k", "有码来赚看主页一天八千"])
async def test_profile_promotion_mutes_permanently_even_with_numeric_body(env, name):
    _, db, tg, svc = env
    await svc.message(message(1, "88886666", **{"from": {"id": 42, "first_name": name, "username": "dRV_dT6"}}))
    await drain(svc)
    assert tg.members[(-1001, 42)]["until_date"] == 0
    assert not tg.members[(-1001, 42)]["can_send_messages"]
    assert [kw["message_id"] for method, kw in tg.calls if method == "delete_message"] == [1]
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
        assert violation.profile_violation and "昵称广告引流：" in violation.reasons[0]
        assert (await session.scalar(select(Action).where(Action.kind == "profile_mute"))).state == "succeeded"


async def test_profile_promotion_is_checked_on_join_and_after_old_member_rename(env):
    _, db, tg, svc = env
    event = join()
    event["new_chat_member"]["user"]["first_name"] = AD_NAME
    await svc.member_event(event)
    await drain(svc)
    assert tg.members[(-1001, 42)]["until_date"] == 0
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=43, joined_at=now()-40*86400, passed_at=now()-40*86400))
    await svc.message(message(2, "66666666", **{"from": {"id": 43, "first_name": AD_NAME}}))
    await drain(svc)
    assert tg.members[(-1001, 43)]["until_date"] == 0


@pytest.mark.parametrize("name", ["普通成员8000", "我的简介", "收款码教程", "有邀请码来交流", "收款码简介每天整理资料", "曝光：有码来赚简介一天8000是骗局", "“有码来赚简介一天8000”广告举报"])
def test_incomplete_profile_signals_reports_and_quotes_are_not_advertisements(name):
    assert not assess("66668888", [], [], default_settings(), sender_name=name)["name_reasons"]


@pytest.mark.parametrize("exemption", ["administrator", "whitelist", "paused", "off"])
async def test_profile_advertising_respects_moderation_protections(env, exemption):
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        if exemption == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        if exemption == "paused":
            group.enabled = False
        if exemption == "off":
            group.settings = {**group.settings, "advertising_ban": False}
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    await svc.message(message(1, "66666666", **{"from": {"id": 42, "first_name": AD_NAME}}))
    await drain(svc)
    assert not any(method in ("restrict_chat_member", "delete_message") for method, _ in tg.calls)


async def test_disabling_profile_advertising_cancels_pending_permanent_mute(env):
    _, db, tg, svc = env
    await svc.message(message(1, "88886666", **{"from": {"id": 42, "first_name": AD_NAME}}))
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "advertising_ban": False}
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)


@pytest.mark.parametrize("archive_enabled", [False, True])
async def test_model_receives_nickname_and_at_username_with_or_without_archive(env, archive_enabled):
    chat, model = await configured(env, archive_enabled=archive_enabled)
    await chat.observe(message(1, "柴郡喵，今天有什么好电影？",
        **{"from": {"id": 42, "first_name": "电影爱好者", "username": "reviewer2026"}}))
    assert await chat.run_one()
    assert model.calls[0][1]["messages"][-1]["name"] == "电影爱好者 @reviewer2026"
    assert "结合目标发送者的昵称、@用户名和正文识别广告" in model.calls[0][0]


async def test_numeric_profile_ad_is_excluded_before_model(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "66668888", **{"from": {"id": 42, "first_name": AD_NAME}}))
    assert not await chat.run_one() and not model.calls


async def test_frozen_history_recovers_username_and_rechecks_archived_profile(env):
    _, db, _, _ = env
    chat, _ = await configured(env)
    async with db.transaction() as session:
        await capture(session, message(1, "今天聊点电影吧", **{"from": {"id": 42, "first_name": AD_NAME, "username": "dRV_dT6"}}))
        result = await chat.contexts.filter(session, -1001, 0,
            [{"id": 1, "speaker_id": 42, "name": "旧昵称", "text": "今天聊点电影吧", "bot": False}], GroupSettings())
        assert result == []


async def test_model_ad_based_on_profile_does_not_blacklist_other_members_normal_text(env):
    chat, model = await configured(env, chat_advertiser_cooldown=False)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    text = "柴郡喵，今天有什么好电影？"
    await chat.observe(message(1, text, **{"from": {"id": 42, "first_name": "可疑简介", "username": "first_user"}}))
    assert await chat.run_one()
    await age_reply(chat)
    model.result = "可以看看你喜欢的题材。"
    await chat.observe(message(2, text, **{"from": {"id": 43, "first_name": "普通成员", "username": "second_user"}}))
    assert await chat.run_one() and len(model.calls) == 2
