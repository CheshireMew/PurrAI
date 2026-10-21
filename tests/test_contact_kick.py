import pytest
from sqlalchemy import select

from conftest import drain, join, message
from purrai.models import Action, Group, Member, Rule, Violation, Whitelist, now
from purrai.rules import CONTACT_REASON, assess
from purrai.service import Service
from test_verification import fixed_challenge, latest


def card(identifier=10, **kwargs):
    return message(identifier, "", contact={
        "phone_number": "+123456789", "first_name": "广告联系人", "user_id": 88,
    }, **kwargs)


@pytest.mark.parametrize("membership", ["unknown", "new", "old"])
@pytest.mark.parametrize("forwarded", [False, True])
async def test_contact_removes_sender_regardless_of_membership_or_legacy_setting(env, membership, forwarded):
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "newcomer_contacts": False, "newcomer_messages": 0, "newcomer_seconds": 0}
        if membership != "unknown":
            joined = now() - (86400 * 30 if membership == "old" else 10)
            session.add(Member(group_id=-1001, user_id=42, joined_at=joined, passed_at=joined, join_observed=True))
    incoming = card()
    if forwarded:
        incoming["forward_origin"] = {"type": "hidden_user", "sender_user_name": "someone", "date": int(now())}
    await svc.message(incoming)
    await drain(svc)
    assert tg.members[(-1001, 42)]["status"] == "left"
    assert [args["user_id"] for name, args in tg.calls if name == "ban_chat_member"] == [42]
    assert [args["user_id"] for name, args in tg.calls if name == "unban_chat_member"] == [42]
    assert [args["message_id"] for name, args in tg.calls if name == "delete_message"] == [10]
    assert not any(name in ("restrict_chat_member", "send_message") for name, _ in tg.calls)
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
        assert violation.reasons == [CONTACT_REASON] and not violation.warning_active
        action = await session.scalar(select(Action).where(Action.kind == "contact_kick"))
        assert action.state == "succeeded" and action.payload["violation_id"] == violation.id
        member = await session.get(Member, (-1001, 42))
        assert not member.present and member.owned_restriction is None
        assert member.join_observed == (membership != "unknown")


async def test_contact_takes_priority_over_profile_and_status_checks(env):
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="推广", scope="name"))
    incoming = card()
    incoming["from"].update(first_name="推广", is_premium=True)
    await svc.message(incoming)
    await drain(svc)
    assert tg.members[(-1001, 42)]["status"] == "left"
    assert not any(name in ("restrict_chat_member", "get_custom_emoji_stickers") for name, _ in tg.calls)
    async with db.sessions() as session:
        assert await session.scalar(select(Action).where(Action.kind == "profile_mute")) is None


async def test_contact_during_verification_cancels_captcha_and_old_buttons(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, svc = env
    tg.calls.clear()
    await svc.message(card())
    await svc.callback({"id": "stale-contact-answer", "from": {"id": 42},
                        "data": f"v:{verification.id}:{verification.nonce}:1"})
    await drain(svc)
    assert tg.members[(-1001, 42)]["status"] == "left"
    assert (await latest(db)).state == "cancelled"
    assert not any(name in ("restrict_chat_member", "send_photo") for name, _ in tg.calls)
    assert {args["message_id"] for name, args in tg.calls if name == "delete_message"} == {10, verification.message_id}


async def test_duplicate_edited_and_multiple_contact_messages_do_not_repeat_kick(env):
    _, db, tg, svc = env
    await svc.message(card())
    await svc.message(card())
    await svc.message(card(), edited=True)
    await svc.message(card(11))
    await drain(svc)
    await svc.message(card())
    await drain(svc)
    assert len([1 for name, _ in tg.calls if name == "ban_chat_member"]) == 1
    assert len([1 for name, _ in tg.calls if name == "delete_message"]) == 2
    async with db.sessions() as session:
        assert len((await session.scalars(select(Violation))).all()) == 2


@pytest.mark.parametrize("method,apply_first", [
    ("ban_chat_member", False), ("ban_chat_member", True),
    ("unban_chat_member", False), ("unban_chat_member", True),
])
async def test_contact_kick_restarts_and_recovers_lost_responses(env, monkeypatch, method, apply_first):
    settings, db, tg, svc = env
    original = tg.call
    failed = False

    async def fail_once(name, **kwargs):
        nonlocal failed
        if name == method and not failed:
            failed = True
            if apply_first:
                await original(name, **kwargs)
            raise ConnectionError("response lost")
        return await original(name, **kwargs)

    monkeypatch.setattr(tg, "call", fail_once)
    await svc.message(card())
    await svc.run_one_action()
    async with db.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "contact_kick"))
        assert action.state == "pending"
        action.available_at = 0
    await drain(Service(db, tg, settings))
    assert tg.members[(-1001, 42)]["status"] == "left"
    assert len([1 for name, _ in tg.calls if name == "ban_chat_member"]) == 1
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "contact_kick"))).state == "succeeded"


@pytest.mark.parametrize("exemption", ["administrator", "creator", "whitelist", "superadmin"])
async def test_contact_exemptions_before_receipt(env, exemption):
    settings, db, tg, svc = env
    if exemption in ("administrator", "creator"):
        tg.members[(-1001, 42)] = {"status": exemption}
    elif exemption == "whitelist":
        async with db.transaction() as session:
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
    else:
        settings.superadmin_ids = [1, 42]
    await svc.message(card())
    await drain(svc)
    assert not any(name in ("delete_message", "ban_chat_member", "unban_chat_member") for name, _ in tg.calls)


@pytest.mark.parametrize("exemption", ["administrator", "whitelist", "disabled", "new_generation"])
async def test_queued_contact_kick_rechecks_current_decision(env, exemption):
    _, db, tg, svc = env
    await svc.message(card())
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    else:
        async with db.transaction() as session:
            if exemption == "whitelist":
                session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
            elif exemption == "disabled":
                (await session.get(Group, -1001)).enabled = False
            else:
                (await session.get(Member, (-1001, 42))).generation = "joined-again"
    await drain(svc)
    assert not any(name in ("ban_chat_member", "unban_chat_member") for name, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "contact_kick"))).state == "cancelled"


async def test_contact_kick_does_not_clear_another_administrators_ban(env):
    _, db, tg, svc = env
    await svc.message(card())
    tg.members[(-1001, 42)] = {"status": "kicked", "until_date": 0}
    await drain(svc)
    assert not any(name in ("ban_chat_member", "unban_chat_member") for name, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "contact_kick"))).state == "failed"


async def test_rejoining_after_contact_kick_gets_fresh_verification(env):
    _, db, tg, svc = env
    await svc.message(card())
    await drain(svc)
    tg.members[(-1001, 42)] = {"status": "member"}
    await svc.member_event(join(date=now() + 1), event_id=30)
    await drain(svc)
    assert (await latest(db)).state == "pending"
    await svc.message(card())
    await drain(svc)
    assert (await latest(db)).state == "pending"
    assert len([1 for name, _ in tg.calls if name == "ban_chat_member"]) == 1


async def test_bots_own_departure_event_does_not_interrupt_contact_unban(env, monkeypatch):
    _, db, tg, svc = env
    original = tg.call

    async def notify_departure(name, **kwargs):
        result = await original(name, **kwargs)
        if name == "ban_chat_member":
            event = join(actor=tg.user_id, old="member", new="kicked")
            event["new_chat_member"]["until_date"] = kwargs["until_date"]
            await svc.member_event(event, event_id=100)
        return result

    monkeypatch.setattr(tg, "call", notify_departure)
    await svc.message(card())
    await drain(svc)
    assert tg.members[(-1001, 42)]["status"] == "left"
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "contact_kick"))).state == "succeeded"


async def test_channel_contact_deletes_card_without_using_user_kick_api(env):
    _, _, tg, svc = env
    await svc.message(card(sender_chat={"id": -2002, "title": "channel"}))
    await drain(svc)
    assert [args["message_id"] for name, args in tg.calls if name == "delete_message"] == [10]
    assert not any(name in ("restrict_chat_member", "ban_chat_member", "unban_chat_member") for name, _ in tg.calls)


async def test_phone_text_does_not_trigger_contact_rule(env):
    _, _, tg, svc = env
    await svc.message(message(10, "联系人：+123456789"))
    await drain(svc)
    assert not any(name in ("delete_message", "ban_chat_member") for name, _ in tg.calls)


async def test_rule_preview_and_violation_results_expose_contact_kick(env, client):
    assert assess("", [], [], {"newcomer_contacts": False}, contact=True)["reasons"] == [CONTACT_REASON]
    _, _, _, svc = env
    await svc.message(card())
    await drain(svc)
    response = await client.get("/api/groups/-1001/violations")
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert any(action["kind"] == "contact_kick" and action["state"] == "succeeded" for action in row["actions"])
    response = await client.post("/api/groups/-1001/rules/preview", json={
        "text": "", "contact": True, "sender_name": "推广", "rules": [
            {"kind": "keyword", "value": "推广", "scope": "name"},
        ],
    })
    assert response.status_code == 200
    assert response.json()["kick_member"] and response.json()["delete_message"]
    assert not response.json()["mute_member"]
