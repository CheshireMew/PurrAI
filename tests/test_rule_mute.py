import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.management import Management
from purrai.models import Action, Member, Rule, Violation, Whitelist, now
from purrai.runtime import Runtime
from purrai.service import Service
from test_bot_admin import click, markup, open_group
from test_verification import fixed_challenge, latest


async def add_rule(db, penalty="permanent_mute"):
    async with db.transaction() as session:
        session.add(Rule(id="ad-rule", group_id=-1001, kind="keyword", value="微信代收日入上万",
                         mode="compact", penalty=penalty))


@pytest.mark.parametrize("variant", ["text", "caption", "edited"])
async def test_selected_body_rule_deletes_and_permanently_mutes(env, variant):
    _, db, tg, svc = env
    await add_rule(db)
    incoming = message(10, "微\u200b信·代收 日入上万")
    if variant == "caption":
        incoming["caption"] = incoming.pop("text")
        incoming["photo"] = [{"file_id": "image"}]
    if variant == "edited":
        await svc.message(message(10, "原始普通内容"))
    await svc.message(incoming, edited=variant == "edited")
    await svc.message(incoming, edited=variant == "edited")
    await drain(svc)
    assert tg.members[(-1001, 42)]["until_date"] == 0
    assert not tg.members[(-1001, 42)]["can_send_messages"]
    assert [args["message_id"] for method, args in tg.calls if method == "delete_message"] == [10]
    assert len([1 for method, _ in tg.calls if method == "restrict_chat_member"]) == 1
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
        assert violation.rule_mute and not violation.profile_violation and not violation.warning_active
        action = await session.scalar(select(Action).where(Action.kind == "rule_mute"))
        assert action.state == "succeeded" and action.payload["violation_id"] == violation.id


async def test_default_keyword_still_only_deletes(env):
    _, db, tg, svc = env
    await add_rule(db, "delete")
    await svc.message(message(10, "微信代收日入上万"))
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert not (await session.scalar(select(Violation))).rule_mute


async def test_pending_verification_does_not_hide_body_mute(env, monkeypatch):
    await fixed_challenge(env, monkeypatch)
    _, db, tg, svc = env
    await add_rule(db)
    tg.calls.clear()
    await svc.message(message(10, "微信代收日入上万"))
    await drain(svc)
    verification = await latest(db)
    assert verification.state == "failed" and verification.failure_action == "permanent_mute"
    assert tg.members[(-1001, 42)]["until_date"] == 0
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)


@pytest.mark.parametrize("exemption", ["administrator", "creator", "whitelist", "superadmin"])
async def test_rule_mute_preserves_exemptions(env, exemption):
    cfg, db, tg, svc = env
    await add_rule(db)
    if exemption in ("administrator", "creator"):
        tg.members[(-1001, 42)] = {"status": exemption}
    elif exemption == "whitelist":
        async with db.transaction() as session:
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
    else:
        cfg.superadmin_ids = [1, 42]
    await svc.message(message(10, "微信代收日入上万"))
    await drain(svc)
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in tg.calls)


@pytest.mark.parametrize("change", ["administrator", "whitelist", "generation"])
async def test_queued_rule_mute_rechecks_role_trust_and_generation(env, change):
    _, db, tg, svc = env
    await add_rule(db)
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, joined_at=now(), passed_at=now()))
    await svc.message(message(10, "微信代收日入上万"))
    if change == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    else:
        async with db.transaction() as session:
            if change == "whitelist":
                session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
            else:
                (await session.get(Member, (-1001, 42))).generation = "new-join"
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "rule_mute"))).state == "cancelled"


async def test_lost_mute_response_recovers_after_restart_without_repeating_restriction(env, monkeypatch):
    cfg, db, tg, svc = env
    await add_rule(db)
    original = tg.call
    failed = False

    async def lose_response(method, **kwargs):
        nonlocal failed
        result = await original(method, **kwargs)
        if method == "restrict_chat_member" and not failed:
            failed = True
            raise ConnectionError("response lost")
        return result

    monkeypatch.setattr(tg, "call", lose_response)
    await svc.message(message(10, "微信代收日入上万"))
    await drain(svc)
    async with db.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "rule_mute"))
        assert action.state == "pending"
        action.available_at = 0
    await drain(Service(db, tg, cfg))
    assert len([1 for method, _ in tg.calls if method == "restrict_chat_member"]) == 1
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "rule_mute"))).state == "succeeded"


async def test_manual_unmute_restores_permissions_and_grants_trust(env):
    cfg, db, tg, svc = env
    await add_rule(db)
    await svc.message(message(10, "微信代收日入上万"))
    await drain(svc)
    await Management(db, tg, svc, cfg).member_action(1, -1001, 42, "unmute", "release-body-ad", "人工确认")
    await drain(svc)
    assert tg.members[(-1001, 42)]["can_send_messages"]
    tg.calls.clear()
    await svc.message(message(11, "微信代收日入上万"))
    await drain(svc)
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in tg.calls)


async def test_contact_rule_still_kicks_and_channel_identity_only_deletes(env):
    _, db, tg, svc = env
    await add_rule(db)
    await svc.message(message(10, "微信代收日入上万", contact={"first_name": "ad", "phone_number": "123"}))
    await svc.message(message(11, "微信代收日入上万", sender_chat={"id": -2002, "title": "channel"}))
    await drain(svc)
    assert [args["user_id"] for method, args in tg.calls if method == "ban_chat_member"] == [42]
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)


async def test_http_penalty_setting_and_preview(env, client):
    response = await client.post("/api/groups/-1001/rules", json={
        "kind": "keyword", "value": "微信代收日入上万", "mode": "compact", "penalty": "permanent_mute",
    })
    assert response.status_code == 200 and response.json()["penalty"] == "permanent_mute"
    response = await client.post("/api/groups/-1001/rules/preview", json={"text": "微信代收 日入上万"})
    assert response.json()["mute_member"] and response.json()["delete_message"]
    invalid = await client.post("/api/groups/-1001/rules", json={
        "kind": "keyword", "value": "广告", "penalty": "ban",
    })
    assert invalid.status_code == 422


async def test_bot_can_set_and_display_rule_penalty(env):
    cfg, db, tg, svc = env
    await add_rule(db, "delete")
    runtime = Runtime(db, tg, svc)
    await open_group(runtime, tg)
    await click(runtime, tg, "广告规则与测试")
    await click(runtime, tg, "微信代收日入上万")
    assert "正文处置改为删消息并永久禁言" in str(markup(tg))
    await click(runtime, tg, "正文处置改为删消息并永久禁言")
    assert "正文违规删消息并永久禁言" in markup(tg)["text"]
    async with db.sessions() as session:
        assert (await session.get(Rule, "ad-rule")).penalty == "permanent_mute"
