import pytest
from sqlalchemy import select

from conftest import drain, join
from purrai import captcha
from purrai.management import Management
from purrai.models import Action, Group, Inbox, Verification, now
from purrai.runtime import Runtime
from purrai.schemas import SettingsPatch
from purrai.service import Service
from test_verification import enter_code, latest


async def configure(env, monkeypatch, method="channel", *, subscribed=False):
    cfg, db, tg, service = env
    original = tg.call

    async def channel(method, **kwargs):
        if method == "get_chat" and kwargs["chat_id"] in ("@ExampleChannel", -2001):
            return {"id": -2001, "type": "channel", "title": "测试频道", "username": "ExampleChannel"}
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", channel)
    tg.members[(-2001, 42)] = {"status": "member" if subscribed else "left"}
    await Management(db, tg, service, cfg).settings_patch(1, -1001, SettingsPatch(settings={
        "verification_method": method, "verification_channel": "https://t.me/ExampleChannel。" if method == "channel" else "",
    }), "set-method")


async def due(db, *, expire=False):
    async with db.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "subscription_check"))
        action.available_at = 0
        if expire:
            (await session.get(Verification, action.payload["verification_id"])).deadline = 0


async def test_text_question_retry_other_user_and_correct_answer(env, monkeypatch):
    await configure(env, monkeypatch, "text")
    cfg, db, tg, service = env
    monkeypatch.setattr("purrai.verification_prompts.text_question", lambda: ("7 + 5 = ？", "12", ["8", "10", "12", "14"]))
    tg.fail["send_message"] = 1
    await service.member_event(join())
    await drain(service)
    first = await latest(db)
    assert first.message_id is None and first.method == "text"
    async with db.transaction() as session:
        (await session.get(Verification, first.id)).deadline = 0
        action = await session.scalar(select(Action).where(Action.kind == "challenge"))
        action.available_at = 0
    await service.scan_timeouts()
    service = Service(db, tg, cfg)
    await drain(service)
    row = await latest(db)
    assert row.state == "pending" and row.deadline > now() + 170
    prompts = [args for method, args in tg.calls if method == "send_message"]
    assert len(prompts) == 2 and prompts[0]["text"] == prompts[1]["text"]
    assert prompts[0]["reply_markup"] == prompts[1]["reply_markup"]
    assert not any(method == "send_photo" for method, _ in tg.calls)
    await enter_code(service, row, "12", user_id=99)
    assert (await latest(db)).state == "pending"
    await enter_code(service, row, "12")
    await drain(service)
    assert (await latest(db)).state == "passed"
    assert tg.members[(-1001, 42)]["can_send_messages"] is True


async def test_text_wrong_answer_replaces_question_and_stale_answer_cannot_pass(env, monkeypatch):
    await configure(env, monkeypatch, "text")
    _, db, _, service = env
    await service.member_event(join())
    await drain(service)
    original = await latest(db)
    wrong = next(value for value in original.choices if captcha.digest(value, original.nonce) != original.answer_hash)
    await enter_code(service, original, wrong)
    await drain(service)
    current = await latest(db)
    assert current.attempts == 1 and current.nonce != original.nonce and current.message_id != original.message_id
    assert current.challenge.get("question") and len(current.choices) == 4
    answer = next(value for value in original.choices if captcha.digest(value, original.nonce) == original.answer_hash)
    await enter_code(service, original, answer)
    assert (await latest(db)).state == "pending"


async def test_existing_subscriber_passes_without_prompt(env, monkeypatch):
    await configure(env, monkeypatch, subscribed=True)
    _, db, tg, service = env
    await service.member_event(join())
    await drain(service)
    assert (await latest(db)).state == "passed"
    assert not any(method in ("send_message", "send_photo") for method, _ in tg.calls)
    assert tg.members[(-1001, 42)]["can_send_messages"] is True


@pytest.mark.parametrize("trigger", ["event", "periodic", "final"])
async def test_subscription_passes_automatically_without_confirmation_button(env, monkeypatch, trigger):
    await configure(env, monkeypatch)
    cfg, db, tg, service = env
    await service.member_event(join())
    await drain(service)
    prompt = next(args for method, args in tg.calls if method == "send_message")
    assert prompt["reply_markup"] == {"inline_keyboard": [[{"text": "订阅频道", "url": "https://t.me/ExampleChannel"}]]}
    tg.members[(-2001, 42)] = {"status": "member"}
    if trigger == "event":
        runtime = Runtime(db, tg, service)
        event = join()
        event["chat"] = {"id": -2001, "type": "channel", "title": "测试频道"}
        event["from"].update(is_bot=False, first_name="成员")
        try:
            await runtime.persist([{"update_id": 70001, "chat_member": event}])
            assert await runtime.process_one("messages")
        finally:
            await runtime.events.close()
            await runtime.chat.close()
        async with db.sessions() as session:
            assert await session.get(Group, -2001) is None
    else:
        service = Service(db, tg, cfg)
        await due(db, expire=trigger == "final")
        await service.scan_timeouts()
        assert (await latest(db)).state == "pending"
    await drain(service)
    assert (await latest(db)).state == "passed"
    assert tg.members[(-1001, 42)]["can_send_messages"] is True


async def test_non_subscriber_timeout_requires_final_successful_lookup(env, monkeypatch):
    await configure(env, monkeypatch)
    cfg, db, tg, service = env
    await service.member_event(join())
    await drain(service)
    await due(db, expire=True)
    original = tg.call

    async def offline(method, **kwargs):
        if method == "get_chat_member" and kwargs["chat_id"] == -2001:
            raise ConnectionError("offline")
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", offline)
    for _ in range(9):
        await due(db)
        await service.run_one_action()
        await service.scan_timeouts()
    async with db.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "subscription_check"))
        assert action.state == "pending" and action.attempts == 9
        assert not await session.scalar(select(Action).where(Action.kind == "verification_failure"))
    assert (await latest(db)).state == "checking"
    assert await service.reconcile_verification_cleanup() == 0
    monkeypatch.setattr(tg, "call", original)
    await due(db)
    await drain(Service(db, tg, cfg))
    assert (await latest(db)).state == "failed"
    assert (await latest(db)).failure_action == "permanent_mute"


async def test_lost_channel_permission_pauses_check_without_punishing_user(env, monkeypatch):
    await configure(env, monkeypatch)
    _, db, tg, service = env
    await service.member_event(join())
    await drain(service)
    original = tg.call

    async def removed(method, **kwargs):
        if method == "get_chat_member" and kwargs["chat_id"] == -2001 and kwargs["user_id"] == tg.user_id:
            return {"status": "member"}
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", removed)
    await due(db, expire=True)
    await drain(service)
    await service.scan_timeouts()
    assert (await latest(db)).state == "checking"
    async with db.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "subscription_check"))
        assert action.state == "failed" and "管理员" in action.error
        assert not await session.scalar(select(Action).where(Action.kind == "verification_failure"))
    from purrai.subscription_verification import membership_changed
    monkeypatch.setattr(tg, "call", original)
    tg.members[(-2001, 42)] = {"status": "member"}
    await membership_changed(db, {"chat": {"id": -2001}}, bot_event=True)
    await drain(service)
    assert (await latest(db)).state == "passed"


async def test_unrelated_channel_membership_is_not_ingested(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    event = join()
    event["chat"] = {"id": -2001, "type": "channel"}
    try:
        await runtime.persist([{"update_id": 70002, "chat_member": event}])
        async with db.sessions() as session:
            assert await session.get(Inbox, 70002) is None
    finally:
        await runtime.events.close()
        await runtime.chat.close()


async def test_channel_configuration_validates_permissions_and_keeps_previous_value(env, monkeypatch):
    from purrai.errors import ManagementError
    await configure(env, monkeypatch)
    cfg, db, tg, service = env
    original = tg.call

    async def removed(method, **kwargs):
        if method == "get_chat_member" and kwargs["chat_id"] == -2001:
            return {"status": "member"}
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", removed)
    management = Management(db, tg, service, cfg)
    with pytest.raises(ManagementError, match="频道管理员"):
        await management.setting(1, -1001, "verification_method", "channel", "invalid-config")
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["verification_channel"] == "@ExampleChannel"
    # Switching away remains possible even after channel permissions are lost.
    await management.setting(1, -1001, "verification_method", "text", "switch-away")
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["verification_method"] == "text"


@pytest.mark.parametrize("method", ["text", "channel"])
async def test_join_request_uses_selected_method_and_approves_only_after_success(env, monkeypatch, method):
    from test_join_requests import request
    await configure(env, monkeypatch, method)
    cfg, db, tg, service = env
    await Management(db, tg, service, cfg).setting(1, -1001, "join_requests", True, "enable-requests")
    tg.members[(-1001, 42)] = {"status": "left"}
    await service.join_request(request())
    await drain(service)
    verification = await latest(db)
    assert verification.flow == "join_request" and verification.state == "pending"
    if method == "text":
        code = next(value for value in verification.choices if captcha.digest(value, verification.nonce) == verification.answer_hash)
        await enter_code(service, verification, code)
    else:
        tg.members[(-2001, 42)] = {"status": "member"}
        await due(db)
    await drain(service)
    assert (await latest(db)).state == "passed"
    assert any(method == "approve_chat_join_request" for method, _ in tg.calls)


async def test_existing_challenge_keeps_method_until_admin_reissues(env, monkeypatch):
    from purrai.schemas import ActionInput
    await configure(env, monkeypatch, "text")
    cfg, db, tg, service = env
    await service.member_event(join())
    await drain(service)
    original = await latest(db)
    management = Management(db, tg, service, cfg)
    await management.setting(1, -1001, "verification_method", "image", "select-image")
    assert (await latest(db)).method == "text"
    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    await management.verification_action(1, original.id, ActionInput(action="reissue", reason="切换验证方式", request_id="new-image"))
    await drain(service)
    row = await latest(db)
    assert row.method == "image" and row.nonce != original.nonce
    assert any(method == "send_photo" for method, _ in tg.calls)
    assert any(method == "delete_message" and args["message_id"] == original.message_id for method, args in tg.calls)


async def test_telegram_settings_can_select_text_and_subscription_mode(env, monkeypatch):
    from test_bot_admin import click, deliver, open_group, private
    await configure(env, monkeypatch, "text")
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    try:
        await open_group(runtime, tg)
        await click(runtime, tg, "验证设置")
        await click(runtime, tg, "订阅频道：")
        await deliver(runtime, {"message": private("@ExampleChannel")})
        await click(runtime, tg, "订阅频道自动验证")
        async with db.sessions() as session:
            assert (await session.get(Group, -1001)).settings["verification_method"] == "channel"
        await click(runtime, tg, "文字算术验证")
        async with db.sessions() as session:
            assert (await session.get(Group, -1001)).settings["verification_method"] == "text"
    finally:
        await runtime.events.close()
        await runtime.chat.close()
