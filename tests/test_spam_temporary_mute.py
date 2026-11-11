import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, Group, Violation, Whitelist, now


@pytest.mark.parametrize("text", ["在线", "今天这句话被同一个人连续发送。"])
async def test_repeated_word_or_sentence_deletes_copies_and_mutes_thirty_minutes(env, text):
    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "repeat_count": 3, "similar_enabled": False, "burst_enabled": False}
    for identifier in (1, 2, 3):
        await service.message(message(identifier, text))
    started = now()
    await drain(service)
    assert sorted(kw["message_id"] for method, kw in tg.calls if method == "delete_message") == [1, 2, 3]
    restrictions = [kw for method, kw in tg.calls if method == "restrict_chat_member"]
    assert len(restrictions) == 1
    assert started + 1795 <= restrictions[0]["until_date"] <= now() + 1801
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert not (await session.scalar(select(Violation))).warning_active
        assert (await session.scalar(select(Action).where(Action.kind == "spam_mute"))).state == "succeeded"


@pytest.mark.parametrize("exemption", ["administrator", "whitelist", "disabled"])
async def test_spam_penalty_respects_existing_exemptions(env, exemption):
    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "repeat_count": 3, "similar_enabled": False, "burst_enabled": False}
        if exemption == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        if exemption == "disabled":
            group.enabled = False
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    for identifier in (1, 2, 3):
        await service.message(message(identifier, "在线"))
    await drain(service)
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in tg.calls)


async def test_disabling_spam_rule_cancels_queued_penalty_and_deletions(env):
    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "repeat_count": 3, "similar_enabled": False, "burst_enabled": False}
    for identifier in (1, 2, 3):
        await service.message(message(identifier, "在线"))
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "repeat_enabled": False}
    await drain(service)
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in tg.calls)


async def test_spam_does_not_overwrite_manual_restriction(env):
    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "repeat_count": 3, "similar_enabled": False, "burst_enabled": False}
    for identifier in (1, 2, 3):
        await service.message(message(identifier, "在线"))
    tg.members[(-1001, 42)] = {"status": "restricted", "is_member": True, "until_date": int(now() + 3600),
                                "can_send_messages": False}
    await drain(service)
    assert not any(method in ("restrict_chat_member", "ban_chat_member") for method, _ in tg.calls)
