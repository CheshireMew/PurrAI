import asyncio
import time

from sqlalchemy import select

from conftest import drain, join, message
from purrai import captcha
from purrai.models import Action, Group, Verification
from purrai.service import Service
from purrai.models import uid


async def latest(database):
    async with database.sessions() as session:
        return await session.scalar(select(Verification).order_by(Verification.created_at.desc()))


async def enter_code(service, verification, code, user_id=42):
    if verification.choices:
        if code not in verification.choices:
            code = next(value for value in verification.choices
                        if captcha.digest(value, verification.nonce) != verification.answer_hash)
        keys = [f"c{verification.choices.index(code)}"]
    else:
        keys = list(code) + ["ok"]
    for key in keys:
        await service.callback(
            {
                "id": f"cb-{uid()}",
                "from": {"id": user_id},
                "data": f"v:{verification.id}:{verification.nonce}:{key}",
            }
        )


async def fixed_challenge(env, monkeypatch):
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, database, _, service = env
    await service.member_event(join())
    await drain(service)
    return await latest(database)


async def legacy_challenge(env, monkeypatch):
    # Exercise the compatibility path for digit keyboards issued before upgrade.
    monkeypatch.setattr(captcha, "make_choices", lambda code: [])
    return await fixed_challenge(env, monkeypatch)


async def test_full_verification_preserves_group_permissions(env, monkeypatch):
    _, database, telegram, service = env
    verification = await fixed_challenge(env, monkeypatch)
    assert verification.state == "pending"
    telegram.defaults["can_send_photos"] = False
    await enter_code(service, verification, "123456")
    assert (await latest(database)).state == "releasing"
    await drain(service)
    assert (await latest(database)).state == "passed"
    assert telegram.members[(-1001, 42)]["can_send_photos"] is False
    assert telegram.members[(-1001, 42)]["can_send_messages"] is True


async def test_other_user_cannot_answer(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await enter_code(service, verification, "123456", user_id=99)
    assert (await latest(database)).input_digits == ""
    assert "只有" in telegram.calls[-1][1]["text"]


async def test_wrong_answer_rotates_nonce_without_extending_deadline(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, _, service = env
    await enter_code(service, verification, "654321")
    changed = await latest(database)
    assert changed.attempts == 1 and changed.nonce != verification.nonce
    assert changed.deadline == verification.deadline
    await drain(service)
    await enter_code(service, verification, "123456")
    assert (await latest(database)).state == "pending"


async def test_three_wrong_answers_permanently_mute_without_removing(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    for _ in range(3):
        await enter_code(service, verification, "654321")
        await drain(service)
        verification = await latest(database)
    assert verification.state == "failed"
    assert telegram.members[(-1001, 42)]["until_date"] == 0
    assert telegram.members[(-1001, 42)]["status"] == "restricted"
    assert verification.failure_action == "permanent_mute"
    assert not any(method == "ban_chat_member" for method, _ in telegram.calls)


async def test_timeout_and_restart_are_durable(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, database, telegram, _ = env
    async with database.transaction() as session:
        row = await session.get(Verification, verification.id)
        row.deadline = time.time() - 1
    restarted = Service(database, telegram, settings)
    await restarted.scan_timeouts()
    await drain(restarted)
    assert (await latest(database)).state == "failed"


async def test_duplicate_join_does_not_create_second_verification(env, monkeypatch):
    _, database, _, service = env
    event = join()
    await asyncio.gather(service.member_event(event), service.member_event(event))
    async with database.sessions() as session:
        assert len((await session.scalars(select(Verification))).all()) == 1


async def test_rejoin_invalidates_old_buttons(env, monkeypatch):
    old = await fixed_challenge(env, monkeypatch)
    _, database, _, service = env
    await service.member_event(join(old="member", new="left"))
    await service.member_event(join(date=int(time.time()) + 1))
    await drain(service)
    await enter_code(service, old, "123456")
    assert (await latest(database)).state == "pending"


async def test_manual_restriction_is_never_overridden(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await enter_code(service, verification, "123456")
    telegram.members[(-1001, 42)]["until_date"] = int(time.time() + 3600)
    await drain(service)
    assert (await latest(database)).state == "releasing"
    async with database.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "release"))
        assert action.state == "failed" and "管理员" in action.error


async def test_release_network_failure_not_reported_as_passed(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, telegram, service = env
    await enter_code(service, verification, "123456")
    telegram.fail["restrict_chat_member"] = 1
    await service.run_one_action()
    assert (await latest(database)).state == "releasing"
    async with database.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "release"))
        action.available_at = 0
    await drain(service)
    assert (await latest(database)).state == "passed"


async def test_pending_member_messages_prioritized_for_deletion(env, monkeypatch):
    await fixed_challenge(env, monkeypatch)
    _, _, telegram, service = env
    await service.message(message(501, "广告"))
    await drain(service)
    assert any(m == "delete_message" and k["message_id"] == 501 for m, k in telegram.calls)


async def test_disabling_verification_keeps_existing_task(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, database, _, service = env
    async with database.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_enabled": False}
    await service.member_event(join(user_id=43))
    async with database.sessions() as session:
        assert len((await session.scalars(select(Verification))).all()) == 1
        assert (await session.get(Verification, verification.id)).state == "pending"


def test_real_captcha_is_readable_png_and_callbacks_fit_telegram():
    from PIL import Image
    import io

    code, image = captcha.make_image()
    assert len(code) == 6 and code.isdigit()
    assert Image.open(io.BytesIO(image)).size == (360, 130)
    assert (
        max(
            len(b["callback_data"].encode())
            for r in captcha.keyboard("a" * 32, "b" * 8, captcha.make_choices(code))["inline_keyboard"]
            for b in r
        )
        <= 64
    )
