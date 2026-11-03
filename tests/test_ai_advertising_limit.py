import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai import chat_commands
from purrai import ai_advertising
from purrai.ai_advertising import advertisement_count, record_ai_advertisement
from purrai.chat_model import ChatReply
from purrai.models import Action, Group, Whitelist
from purrai.service import Service
from test_chat import age_reply, configured


async def test_three_actual_model_verdicts_within_window_persist_and_permanently_mute(env, monkeypatch):
    chat, model = await configured(env)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    instant = [chat_commands.now()]
    monkeypatch.setattr(chat_commands, "now", lambda: instant[0])
    for identifier in (1, 2, 3):
        await chat.observe(message(identifier, f"这是一段交给模型判断的推广内容{identifier}"))
        assert await chat.run_one()
        async with chat.db.sessions() as session:
            assert await advertisement_count(session, -1001, 42) == identifier
        await age_reply(chat)
        instant[0] += 1801
        if identifier < 3:
            assert not any(method == "restrict_chat_member" for method, _ in chat.tg.calls)
    assert len(model.calls) == 3
    restored = Service(chat.db, chat.tg, env[0])
    await drain(restored)
    assert chat.tg.members[(-1001, 42)]["until_date"] == 0
    assert not chat.tg.members[(-1001, 42)]["can_send_messages"]
    assert not any(method in ("delete_message", "ban_chat_member") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "ai_ad_mute"))).state == "succeeded"


async def test_cached_ad_and_duplicate_model_result_do_not_add_judgments(env):
    chat, model = await configured(env)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    text = "这是一段交给模型判断的推广内容"
    await chat.observe(message(1, text))
    assert await chat.run_one()
    await chat.observe(message(2, text))
    assert not await chat.run_one()
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 1, text, "成员")
        assert await advertisement_count(session, -1001, 42) == 1
    assert len(model.calls) == 1


@pytest.mark.parametrize("exemption", ["administrator", "whitelist", "disabled", "off"])
async def test_ai_limit_respects_existing_protections_and_current_switch(env, exemption):
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        if exemption == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        if exemption == "disabled":
            group.enabled = False
        for identifier in (1, 2, 3):
            await record_ai_advertisement(session, group, 42, identifier, "模型判定的广告内容", "成员")
        if exemption == "off":
            group.settings = {**group.settings, "chat_advertiser_cooldown": False}
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    await drain(svc)
    assert not any(method in ("restrict_chat_member", "ban_chat_member", "delete_message") for method, _ in tg.calls)


async def test_ai_counters_are_per_group_and_disabled_mode_does_not_count(env):
    chat, model = await configured(env, chat_advertiser_cooldown=False)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    await chat.observe(message(1, "这是一段交给模型判断的推广内容"))
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        assert await advertisement_count(session, -1001, 42) == 0
        assert await advertisement_count(session, -1002, 42) == 0


async def test_two_judgments_in_one_group_and_one_in_another_do_not_mute(env):
    _, db, tg, _ = env
    async with db.transaction() as session:
        first = await session.get(Group, -1001)
        second = Group(id=-1002, approved=True, enabled=True, settings=first.settings)
        session.add(second)
        await session.flush()
        await record_ai_advertisement(session, first, 42, 1, "广告甲", "成员")
        await record_ai_advertisement(session, first, 42, 2, "广告乙", "成员")
        await record_ai_advertisement(session, second, 42, 1, "广告丙", "成员")
        assert await advertisement_count(session, -1001, 42) == 2
        assert await advertisement_count(session, -1002, 42) == 1
        assert await session.scalar(select(Action)) is None


async def test_reenabling_after_cancelled_threshold_waits_for_a_new_actual_verdict(env):
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        for identifier in (1, 2, 3):
            await record_ai_advertisement(session, group, 42, identifier, "广告内容", "成员")
        group.settings = {**group.settings, "chat_advertiser_cooldown": False}
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_advertiser_cooldown": True}
        await record_ai_advertisement(session, group, 42, 4, "新的广告内容", "成员")
    await drain(svc)
    assert tg.members[(-1001, 42)]["until_date"] == 0


async def test_three_day_window_expires_each_old_judgment_individually(env, monkeypatch):
    _, db, _, _ = env
    instant = [ai_advertising.now()]
    monkeypatch.setattr(ai_advertising, "now", lambda: instant[0])
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 1, "广告甲", "成员")
    instant[0] += 2 * 86400
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 2, "广告乙", "成员")
        assert await advertisement_count(session, -1001, 42) == 2
    instant[0] += 86400
    async with db.transaction() as session:
        assert await advertisement_count(session, -1001, 42) == 1
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 3, "广告丙", "成员")
        assert await advertisement_count(session, -1001, 42) == 2
        assert await session.scalar(select(Action)) is None
    instant[0] += ai_advertising.WINDOW
    async with db.sessions() as session:
        assert await advertisement_count(session, -1001, 42) == 0


async def test_expired_threshold_cancels_penalty_waiting_in_queue(env, monkeypatch):
    _, db, tg, svc = env
    instant = [ai_advertising.now()]
    monkeypatch.setattr(ai_advertising, "now", lambda: instant[0])
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        for identifier in (1, 2, 3):
            await record_ai_advertisement(session, group, 42, identifier, "广告内容", "成员")
    instant[0] += ai_advertising.WINDOW
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "ai_ad_mute"))).state == "cancelled"
