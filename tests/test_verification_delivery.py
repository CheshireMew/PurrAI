import pytest
from sqlalchemy import select

from conftest import drain, join
from purrai.management import Management
from purrai.models import Action, Group, Member, Verification
from purrai.schemas import ActionInput
from test_verification import fixed_challenge


async def test_initial_mute_checks_member_once_before_restriction(env):
    _, _, telegram, service = env
    await service.member_event(join())
    await service.run_one_action(include_progress=False)
    assert [method for method, _ in telegram.calls] == [
        "get_chat_member", "get_chat", "restrict_chat_member",
    ]


async def test_restricted_join_uses_current_permissions_without_group_lookup(env):
    _, database, telegram, service = env
    telegram.members[(-1001, 42)] = {
        "status": "restricted", "is_member": True, "until_date": 0,
        **telegram.defaults, "can_send_photos": False,
    }
    await service.member_event(join())
    await service.run_one_action(include_progress=False)
    assert [method for method, _ in telegram.calls] == ["get_chat_member", "restrict_chat_member"]
    async with database.sessions() as session:
        verification = await session.scalar(select(Verification))
        assert verification.previous_permissions["can_send_messages"] is True
        assert verification.previous_permissions["can_send_photos"] is False


async def test_reissue_reuses_confirmed_mute_without_restricting_again(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, service = env
    telegram.calls.clear()
    await Management(database, telegram, service, settings).verification_action(
        1, verification.id, ActionInput(action="reissue", reason="重出题", request_id="delivery-reissue"),
    )
    await drain(service)
    assert not any(method in ("restrict_chat_member", "get_chat") for method, _ in telegram.calls)
    assert sum(method == "send_photo" for method, _ in telegram.calls) == 1


@pytest.mark.parametrize("lost", [False, True])
async def test_photo_delivery_has_separate_timings_and_longer_receipt_timeout(env, monkeypatch, caplog, lost):
    _, database, telegram, _ = env
    if lost:
        telegram.fail["send_photo"] = 1
    clock = iter([0, 0.01, 0.01, 5.01])
    monkeypatch.setattr("purrai.verification_actions.perf_counter", lambda: next(clock))
    verification = await fixed_challenge(env, monkeypatch)
    async with database.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "challenge"))
        assert action.payload["challenge_delivery"] == ("uncertain" if lost else "confirmed")
        timing = action.payload["timing"]
        assert timing["queue_seconds"] >= 0 and timing["preparation_seconds"] >= 0
        assert timing["image_seconds"] == 0.01 and timing["send_seconds"] == 5
        assert timing["send_finished_at"] >= timing["send_started_at"]
        assert (await session.get(Member, (-1001, 42))).present
    photo = next(args for method, args in telegram.calls if method == "send_photo")
    assert photo["request_timeout"] == 30
    assert "send=5.000s" in caplog.text and action.id in caplog.text
    assert verification.state == "pending"


async def test_admin_with_stale_conflict_still_exempts_before_initial_mute(env):
    _, database, telegram, service = env
    await service.member_event(join())
    async with database.transaction() as session:
        (await session.get(Member, (-1001, 42))).restriction_conflict = True
        assert (await session.get(Group, -1001)).enabled
    telegram.members[(-1001, 42)] = {"status": "administrator"}
    await drain(service)
    assert not any(method == "restrict_chat_member" for method, _ in telegram.calls)
    async with database.sessions() as session:
        assert (await session.scalar(select(Verification))).state == "passed"
