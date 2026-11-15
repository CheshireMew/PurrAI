import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, Group, Member, Violation, Whitelist, now


async def test_configured_hour_window_catches_ten_to_thirty_minute_repeats(env, client):
    import json
    from pathlib import Path

    settings = json.loads((Path(__file__).resolve().parents[1] / "data/policies/slow-spam-20261115.json").read_text(encoding="utf-8"))["group_settings"]
    response = await client.patch("/api/groups/-1001/settings", json={"settings": settings})
    assert response.status_code == 200, response.text
    _, db, tg, service = env
    instant = now()
    # Old copies outside the configured window must not count or get removed.
    for identifier, age in ((10, 3700), (11, 1800), (12, 600), (13, 0)):
        await service.message({**message(identifier, "会网购的来"), "date": int(instant - age)})
        if identifier < 13:
            async with db.sessions() as session:
                assert await session.scalar(select(Action).where(Action.kind == "spam_mute")) is None
    await drain(service)
    assert sorted(data["message_id"] for method, data in tg.calls if method == "delete_message") == [11, 12, 13]
    assert sum(method == "restrict_chat_member" for method, _ in tg.calls) == 1
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)


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


@pytest.mark.parametrize("duration", [0, 7200, 600, 1820])
async def test_spam_adds_mute_and_restores_only_its_own_restriction(env, monkeypatch, duration):
    from purrai import actions, restriction_actions
    _, db, tg, service = env
    instant = [now()]
    monkeypatch.setattr(actions, "now", lambda: instant[0])
    monkeypatch.setattr(restriction_actions, "now", lambda: instant[0])
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "repeat_count": 3, "similar_enabled": False, "burst_enabled": False}
    for identifier in (1, 2, 3):
        await service.message(message(identifier, "普通重复消息"))
    instant[0] = now() + 1
    prior_until = int(instant[0] + duration) if duration else 0
    tg.members[(-1001, 42)] = {"status": "restricted", "is_member": True, "until_date": prior_until,
                              **tg.defaults, "can_invite_users": False}
    async with db.transaction() as session:
        (await session.get(Member, (-1001, 42))).restriction_conflict = True
    await drain(service)
    assert not tg.members[(-1001, 42)]["can_send_messages"]
    assert not tg.members[(-1001, 42)]["can_invite_users"]
    async with db.sessions() as session:
        restore = await session.scalar(select(Action).where(Action.kind == "unmute"))
        if duration == 600:
            assert restore.payload["expire_previous"]
            assert tg.members[(-1001, 42)]["until_date"] == int(instant[0] + 1800)
        else:
            assert restore is not None and restore.available_at == int(instant[0] + 1800)
            assert tg.members[(-1001, 42)]["until_date"] == prior_until
    # A future restore must not hold up newer work for the same member.
    from purrai.action_queue import enqueue
    async with db.transaction() as session:
        following = await enqueue(session, "unban", "later-task", -1001, 42, {"request_cooldown": True})
    await drain(service)
    async with db.sessions() as session:
        assert (await session.get(Action, following.id)).state == "succeeded"
    instant[0] += (601 if duration == 600 else 1801)
    if duration == 1820:
        await drain(service)
        assert not tg.members[(-1001, 42)]["can_send_messages"]
        instant[0] = prior_until + 1
        await drain(service)
        assert tg.members[(-1001, 42)]["can_send_messages"]
        assert tg.members[(-1001, 42)]["can_invite_users"]
        assert tg.members[(-1001, 42)]["until_date"] == 0
        return
    tg.fail["restrict_chat_member"] = 1
    await drain(service)
    assert not tg.members[(-1001, 42)]["can_send_messages"]
    instant[0] += 10
    await drain(service)
    assert tg.members[(-1001, 42)]["can_send_messages"] is (duration != 600)
    assert tg.members[(-1001, 42)]["can_invite_users"] is (duration == 600)
    if duration == 600:
        return  # PurrAI's remaining mute ends by Telegram's native expiry.
    assert tg.members[(-1001, 42)]["until_date"] == prior_until
    async with db.sessions() as session:
        assert (await session.get(Member, (-1001, 42))).owned_restriction is None


@pytest.mark.parametrize("replacement", ["administrator", "permanent_penalty", "before_send_failure", "lost_receipt"])
async def test_scheduled_restore_checks_ownership_and_recovers_delivery(env, monkeypatch, replacement):
    from purrai.action_queue import enqueue
    from purrai import actions, restriction_actions
    _, db, tg, service = env
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42))
        await enqueue(session, "manual_mute", "partial-overlay", -1001, 42, {"duration_seconds": 1800})
    tg.members[(-1001, 42)] = {"status": "restricted", "is_member": True, "until_date": 0,
                              **tg.defaults, "can_invite_users": False}
    if replacement == "before_send_failure":
        tg.fail["restrict_chat_member"] = 1
        await drain(service)
        async with db.transaction() as session:
            (await session.scalar(select(Action).where(Action.kind == "manual_mute"))).available_at = 0
    await drain(service)
    assert not tg.members[(-1001, 42)]["can_send_messages"]
    async with db.transaction() as session:
        restore = await session.scalar(select(Action).where(Action.kind == "unmute"))
        instant = [restore.available_at + 1]
        if replacement == "permanent_penalty":
            member = await session.get(Member, (-1001, 42))
            member.owned_restriction = {**member.owned_restriction, "kind": "ai_ad", "source_action": "new-penalty"}
        elif replacement == "administrator":
            (await session.get(Member, (-1001, 42))).restriction_conflict = True
            tg.members[(-1001, 42)]["can_pin_messages"] = False
    monkeypatch.setattr(actions, "now", lambda: instant[0])
    monkeypatch.setattr(restriction_actions, "now", lambda: instant[0])
    sent = len([1 for method, _ in tg.calls if method == "restrict_chat_member"])
    if replacement == "lost_receipt":
        original = tg.call
        failed = False
        async def lose_once(method, **kwargs):
            nonlocal failed
            result = await original(method, **kwargs)
            if method == "restrict_chat_member" and not failed:
                failed = True
                raise ConnectionError("response lost after acceptance")
            return result
        monkeypatch.setattr(tg, "call", lose_once)
    await drain(service)
    instant[0] += 10
    await drain(service)
    if replacement in ("administrator", "permanent_penalty"):
        assert len([1 for method, _ in tg.calls if method == "restrict_chat_member"]) == sent
        assert not tg.members[(-1001, 42)]["can_send_messages"]
    else:
        assert tg.members[(-1001, 42)]["can_send_messages"]
        assert not tg.members[(-1001, 42)]["can_invite_users"]
        assert len([1 for method, _ in tg.calls if method == "restrict_chat_member"]) == sent + 1
