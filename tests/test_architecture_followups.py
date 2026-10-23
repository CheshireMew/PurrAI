"""Regression cases from the second frontend/backend architecture review."""

import asyncio

import pytest
from sqlalchemy import func, select

from conftest import drain, message
from purrai.management import Management, ManagementError
from purrai.models import Action, Fingerprint, Group, Member, MemberText, Rule, Verification, now
from purrai.schemas import ActionInput, SettingsPatch
from purrai.service import Service
from test_verification import enter_code, fixed_challenge, latest
from test_chat import configured, due


async def reissue(env, identifier, request):
    settings, db, tg, service = env
    return await Management(db, tg, service, settings).verification_action(
        1, identifier, ActionInput(action="reissue", reason="重新验证", request_id=request)
    )


async def test_reissued_verification_can_fail_again_after_restart(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, db, tg, service = env
    for cycle in range(2):
        if cycle:
            await reissue(env, verification.id, f"reissue-{cycle}")
            await drain(service)
        async with db.transaction() as session:
            (await session.get(Verification, verification.id)).deadline = now() - 1
        service = Service(db, tg, settings)
        await service.scan_timeouts()
        await drain(service)
        assert (await latest(db)).state == "failed"
    async with db.sessions() as session:
        actions = (await session.scalars(select(Action).where(Action.kind == "verification_failure"))).all()
        assert len(actions) == 2
        assert all(action.state == "succeeded" for action in actions)


async def test_reissue_after_correct_answer_cancels_old_release_and_completes_new_one(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, service = env
    await enter_code(service, verification, "123456")
    await reissue(env, verification.id, "replace-pending-release")
    await drain(service)
    fresh = await latest(db)
    assert fresh.state == "pending"
    await enter_code(service, fresh, "123456")
    await drain(service)
    assert (await latest(db)).state == "passed"
    assert tg.members[(-1001, 42)]["can_send_messages"]
    async with db.sessions() as session:
        assert sorted(await session.scalars(select(Action.state).where(Action.kind == "release"))) == [
            "cancelled", "succeeded",
        ]


async def test_reissue_gives_a_fresh_attempt_budget_but_wrong_answers_do_not_reset_it(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, _, service = env
    for _ in range(3):
        await enter_code(service, verification, "654321")
        await drain(service)
        verification = await latest(db)
    assert verification.state == "failed" and verification.attempts == 3
    receipt = await reissue(env, verification.id, "fresh-attempt-budget")
    assert (await reissue(env, verification.id, "fresh-attempt-budget"))["id"] == receipt["id"]
    await drain(service)
    fresh = await latest(db)
    assert fresh.state == "pending" and fresh.attempts == 0 and not fresh.failure_action
    await enter_code(service, fresh, "654321")
    await drain(service)
    fresh = await latest(db)
    assert fresh.state == "pending" and fresh.attempts == 1
    await enter_code(service, fresh, "123456")
    await drain(service)
    assert (await latest(db)).state == "passed"


@pytest.mark.parametrize("lookup", [2, 3])
async def test_pausing_during_rule_mute_lookup_prevents_the_punishment(env, monkeypatch, lookup):
    settings, db, tg, service = env
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="SPAM", penalty="permanent_mute"))
    await service.message(message(20, "SPAM"))
    # Delete is an earlier independent action. Pause during the mute's own
    # second member lookup or its subsequent default-permissions lookup.
    async with db.transaction() as session:
        (await session.scalar(select(Action).where(Action.kind == "delete"))).state = "succeeded"
    calls = 0
    original = tg.call

    async def pause(method, **kwargs):
        nonlocal calls
        if method in ("get_chat_member", "get_chat"):
            calls += 1
            if calls == lookup:
                await Management(db, tg, service, settings).settings_patch(1, -1001, SettingsPatch(enabled=False))
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", pause)
    await drain(service)
    assert calls >= lookup
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "rule_mute"))).state == "cancelled"


async def test_pause_during_an_accepted_contact_ban_still_records_the_ban(env, monkeypatch):
    settings, db, tg, service = env
    await service.message(message(20, "", contact={"phone_number": "12345", "first_name": "示例"}))
    original = tg.call

    async def pause(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "ban_chat_member":
            await Management(db, tg, service, settings).settings_patch(1, -1001, SettingsPatch(enabled=False))
        return result

    monkeypatch.setattr(tg, "call", pause)
    await drain(service)
    assert tg.members[(-1001, 42)]["status"] == "kicked"
    assert not any(method == "unban_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "contact_ban"))).state == "succeeded"


async def test_revoke_during_telegram_activation_check_cannot_write_active_status(env, monkeypatch):
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    started, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "get_chat_member" and kwargs["user_id"] == tg.user_id:
            started.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    activation = asyncio.create_task(management.permissions(1, -1001, activate=True))
    await asyncio.wait_for(started.wait(), 2)
    try:
        await management.group_access(1, -1001, False)
    finally:
        finish.set()
    with pytest.raises(ManagementError) as error:
        await activation
    assert error.value.status == 403
    async with db.sessions() as session:
        group = await session.get(Group, -1001)
        assert not group.approved and not group.enabled and group.status != "active"
        assert not await session.scalar(select(func.count()).select_from(Action).where(Action.kind == "activate"))


@pytest.mark.parametrize("operation", ["settings", "setting", "rule", "release_pending", "member", "verification"])
async def test_group_mutations_recheck_admission_at_commit_boundary(env, monkeypatch, operation):
    verification = await fixed_challenge(env, monkeypatch)
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    original = management.access.authorize

    async def revoke_after_authorization(*args, **kwargs):
        group = await original(*args, **kwargs)
        await management.group_access(1, -1001, False)
        return group

    monkeypatch.setattr(management.access, "authorize", revoke_after_authorization)
    from purrai.schemas import RuleInput
    body = ActionInput(action="release", reason="人工核实", request_id=f"after-revoke-{operation}")
    requests = {
        "settings": lambda: management.settings_patch(1, -1001, SettingsPatch(settings={"max_attempts": 5})),
        "setting": lambda: management.setting(1, -1001, "max_attempts", 5, "late-setting"),
        "rule": lambda: management.rule(1, -1001, RuleInput(kind="keyword", value="SPAM")),
        "release_pending": lambda: management.release_pending(1, -1001, body),
        "member": lambda: management.member_action(1, -1001, 43, "mute", "late-member"),
        "verification": lambda: management.verification_action(1, verification.id, body),
    }
    with pytest.raises(ManagementError) as error:
        await requests[operation]()
    assert error.value.status == 403
    async with db.sessions() as session:
        group = await session.get(Group, -1001)
        assert not group.approved and not group.enabled
        assert group.settings["max_attempts"] == 3
        assert not await session.scalar(select(func.count()).select_from(Rule))


async def test_text_credit_is_bounded_and_retains_earliest_distinct_contributions(env):
    from purrai.media_gate import remember_text, text_count_before

    _, db, _, _ = env
    async with db.transaction() as session:
        for identifier in range(101, 231):
            await remember_text(session, message(identifier, f"正常文字 {identifier}"))
        # A delayed original must replace the last retained contribution.
        await remember_text(session, message(1, "迟到的第一句"))
        await remember_text(session, message(2, "迟到的第一句"))
        assert await text_count_before(session, message(500, ""), 100) == 100
        assert await text_count_before(session, message(150, ""), 100) == 50
    async with db.sessions() as session:
        retained = list(await session.scalars(select(MemberText.message_id).order_by(MemberText.message_id)))
        assert retained == [1, *range(101, 200)]


async def test_fingerprints_keep_initial_positions_and_bounded_recent_window(env):
    from purrai.state_retention import prune_expired_fingerprints

    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "archive_enabled": False, "repeat_enabled": False,
                          "similar_enabled": False, "burst_enabled": False}
        session.add(Member(group_id=-1001, user_id=42, generation="current", observed_messages=700,
                           joined_at=now() - 86400, passed_at=now() - 86400))
        for identifier in range(1, 701):
            session.add(Fingerprint(group_id=-1001, user_id=42, message_id=identifier,
                                    generation="current", sequence=identifier, digest=str(identifier),
                                    batch_key=f"m:{identifier}", created_at=now() - 100))
    await service.message(message(701, "正常的新消息"))
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Fingerprint)) == 600
        assert (await session.get(Member, (-1001, 42))).observed_messages == 701
    async with db.transaction() as session:
        for item in await session.scalars(select(Fingerprint)):
            item.created_at = now() - 7200
        await prune_expired_fingerprints(session)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Fingerprint)) == 100
    # An edit of the very first message retains the original newcomer position;
    # an edit whose fingerprint expired never earns another observed message.
    await service.message(message(1, "https://outside.example"), edited=True)
    await service.message(message(500, "编辑历史正常文字"), edited=True)
    await drain(service)
    assert any(method == "delete_message" and args["message_id"] == 1 for method, args in tg.calls)
    async with db.sessions() as session:
        assert (await session.get(Member, (-1001, 42))).observed_messages == 701


async def test_unseen_historical_edits_cannot_fill_permanent_first_message_slots(env):
    from purrai.state_retention import prune_expired_fingerprints

    _, db, _, service = env
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, generation="joined", observed_messages=0,
                           joined_at=now() - 86400))
    for identifier in (1001, 1002):
        await service.message(message(identifier, f"历史编辑 {identifier}", date=int(now() - 7200)), edited=True)
    async with db.transaction() as session:
        await prune_expired_fingerprints(session)
        assert (await session.get(Member, (-1001, 42))).observed_messages == 0
        assert not await session.scalar(select(func.count()).select_from(Fingerprint))


async def test_one_busy_topic_with_over_100_turns_cannot_starve_another_group(env):
    chat, model = await configured(env)
    _, db, tg, _ = env
    async with db.transaction() as session:
        source = await session.get(Group, -1001)
        session.add(Group(id=-1002, approved=True, enabled=True, settings=source.settings))
    held = asyncio.Event()
    model.release = held
    await chat.observe(message(1, "@purrai_bot 第一个问题"))
    await due(chat)
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 2)
        for identifier in range(2, 103):
            await chat.observe(message(identifier, f"@purrai_bot 排队问题 {identifier}"))
        await chat.observe(message(1, "@purrai_bot 另一群的问题", chat={"id": -1002, "type": "supergroup"}))
        await due(chat)
        # The second real worker must reach the model and send in the other
        # group while the first worker is still waiting on its model response.
        model.release = None
        assert await asyncio.wait_for(chat.run_one(), 5)
        assert any(method == "send_message" and args["chat_id"] == -1002 for method, args in tg.calls)
        assert not any(method == "send_message" and args["chat_id"] == -1001 for method, args in tg.calls)
    finally:
        held.set()
        await running


async def test_a_cooled_topic_with_over_100_turns_does_not_hide_another_group(env):
    import json
    from purrai.models import Meta

    chat, _ = await configured(env)
    _, db, tg, _ = env
    async with db.transaction() as session:
        source = await session.get(Group, -1001)
        session.add(Group(id=-1002, approved=True, enabled=True, settings=source.settings))
        session.add(Meta(key="chat_session:-1001:0", value=json.dumps({"blocked_until": now() + 60})))
    for identifier in range(1, 102):
        await chat.observe(message(identifier, f"@purrai_bot 等待问题 {identifier}"))
    await chat.observe(message(1, "@purrai_bot 请回应", chat={"id": -1002, "type": "supergroup"}))
    await due(chat)
    assert await chat.run_one()
    assert [args["chat_id"] for method, args in tg.calls if method == "send_message"] == [-1002]
