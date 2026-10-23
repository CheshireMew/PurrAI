import copy

import pytest
from sqlalchemy import select

from conftest import drain, join
from purrai.models import Action, Member, Verification
from purrai.service import Service
from test_verification import enter_code, fixed_challenge, latest


def restricted_transition(telegram, *, entering, date):
    event = join(date=date, actor=42, old="restricted", new="restricted")
    permissions = telegram.members[(-1001, 42)]
    for field, present in (("old_chat_member", not entering), ("new_chat_member", entering)):
        event[field].update(copy.deepcopy(permissions), is_member=present)
    return event


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("departure", ["observed", "missed", "restart"])
async def test_rejoin_during_verification_restores_permissions_before_first_mute(
    env, monkeypatch, departure, failed,
):
    original = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, service = env
    if failed:
        async with database.transaction() as session:
            (await session.get(Verification, original.id)).deadline = 0
        await service.scan_timeouts()
        await drain(service)
        assert (await latest(database)).failure_action == "permanent_mute"
    date = int(original.created_at) + 1
    if departure != "missed":
        await service.member_event(restricted_transition(telegram, entering=False, date=date), event_id=10)
    if departure == "restart":
        service = Service(database, telegram, settings)
    await service.member_event(restricted_transition(telegram, entering=True, date=date + 1), event_id=11)
    await drain(service)
    current = await latest(database)
    assert current.id != original.id
    await enter_code(service, original, "123456")
    assert (await latest(database)).state == "pending"
    await enter_code(service, current, "123456")
    await drain(service)
    assert (await latest(database)).state == "passed"
    assert telegram.members[(-1001, 42)]["can_send_messages"] is True
    async with database.sessions() as session:
        assert (await session.get(Member, (-1001, 42))).owned_restriction is None
        assert (await session.get(Verification, current.id)).previous_permissions["can_send_messages"] is True


async def test_rejoin_keeps_an_actual_preexisting_media_restriction(env, monkeypatch):
    _, database, telegram, service = env
    telegram.members[(-1001, 42)] = {
        "status": "restricted", "is_member": True, "until_date": 0,
        **telegram.defaults, "can_send_photos": False,
    }
    original = await fixed_challenge(env, monkeypatch)
    date = int(original.created_at) + 1
    await service.member_event(restricted_transition(telegram, entering=False, date=date), event_id=10)
    await service.member_event(restricted_transition(telegram, entering=True, date=date + 1), event_id=11)
    await drain(service)
    await enter_code(service, await latest(database), "123456")
    await drain(service)
    assert telegram.members[(-1001, 42)]["can_send_messages"] is True
    assert telegram.members[(-1001, 42)]["can_send_photos"] is False


async def test_rejoin_does_not_claim_another_administrators_changed_mute(env, monkeypatch):
    _, database, telegram, service = env
    original = await fixed_challenge(env, monkeypatch)
    date = int(original.created_at) + 1
    await service.member_event(restricted_transition(telegram, entering=False, date=date), event_id=10)
    telegram.members[(-1001, 42)]["until_date"] = date + 3600
    await service.member_event(restricted_transition(telegram, entering=True, date=date + 1), event_id=11)
    await drain(service)
    current = await latest(database)
    await enter_code(service, current, "123456")
    await drain(service)
    assert telegram.members[(-1001, 42)]["can_send_messages"] is False
    assert telegram.members[(-1001, 42)]["until_date"] == date + 3600
    async with database.sessions() as session:
        release = await session.scalar(select(Action).where(Action.kind == "release"))
        assert release.state == "succeeded"


async def test_rejoin_preserves_a_group_rule_penalty(env, monkeypatch):
    _, database, telegram, service = env
    original = await fixed_challenge(env, monkeypatch)
    async with database.transaction() as session:
        member = await session.get(Member, (-1001, 42))
        member.owned_restriction = {**member.owned_restriction, "kind": "rule", "source_action": "ad-penalty"}
    date = int(original.created_at) + 1
    await service.member_event(restricted_transition(telegram, entering=False, date=date), event_id=10)
    await service.member_event(restricted_transition(telegram, entering=True, date=date + 1), event_id=11)
    await drain(service)
    await enter_code(service, await latest(database), "123456")
    await drain(service)
    assert telegram.members[(-1001, 42)]["can_send_messages"] is False
